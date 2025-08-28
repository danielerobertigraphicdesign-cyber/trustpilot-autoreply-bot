
import os, json, sqlite3, hashlib, uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import pytz
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

TP_API_BASE = os.getenv("TP_API_BASE", "https://api.trustpilot.com")
TP_BUSINESS_TOKEN = os.getenv("TP_BUSINESS_TOKEN", "")
TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Rome")
APP_APPROVAL_MODE = os.getenv("APP_APPROVAL_MODE", "true").lower() == "true"
APP_APPROVAL_CHANNEL = os.getenv("APP_APPROVAL_CHANNEL", "none")
APP_APPROVAL_WEBHOOK = os.getenv("APP_APPROVAL_WEBHOOK", "")
TEMPLATES_PATH = os.path.join(os.path.dirname(__file__), "templates.json")

ALERT_CHANNEL = os.getenv("ALERT_CHANNEL", "none")
ALERT_SLACK_WEBHOOK = os.getenv("ALERT_SLACK_WEBHOOK", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() == "true"

async def send_slack_alert(text: str):
    if ALERT_CHANNEL in ("slack", "both") and ALERT_SLACK_WEBHOOK:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                await client.post(ALERT_SLACK_WEBHOOK, json={"text": text})
            except Exception:
                pass

def send_email_alert(subject: str, body: str):
    if ALERT_CHANNEL in ("email", "both") and ALERT_EMAIL_TO and SMTP_HOST and SMTP_USER and SMTP_PASS:
        msg = f"From: {SMTP_USER}\r\nTo: {ALERT_EMAIL_TO}\r\nSubject: {subject}\r\n\r\n{body}"
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                if SMTP_TLS:
                    server.starttls(context=context)
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, [ALERT_EMAIL_TO], msg.encode("utf-8"))
        except Exception:
            pass

async def alert_error(title: str, detail: str):
    text = f":warning: {title}\n{detail}"
    await send_slack_alert(text)
    send_email_alert(title, detail)


app = FastAPI(title="Trustpilot Auto-Reply Bot")
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.sqlite3")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS replies (
    review_id TEXT PRIMARY KEY,
    status TEXT,
    template_key TEXT,
    lang TEXT,
    stars INTEGER,
    period TEXT,
    message_hash TEXT,
    created_at TEXT
)""")
conn.commit()

with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
    TEMPLATES = json.load(f)

def local_age_days(created_at_iso: str) -> int:
    dt = datetime.fromisoformat(created_at_iso.replace("Z","+00:00"))
    tz = pytz.timezone(TIMEZONE)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    local_dt = dt.astimezone(tz)
    delta = local_now - local_dt
    return max(0, delta.days)

def period_from_age(age_days: int) -> str:
    return "Fresco" if age_days <= 5 else "Vecchio"

def already_replied(review_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM replies WHERE review_id = ?", (review_id,))
    return cur.fetchone() is not None

def save_log(review_id: str, status: str, template_key: str, lang: str, stars: int, period: str, message: str):
    mh = hashlib.sha256(message.encode("utf-8")).hexdigest()
    conn.execute("REPLACE INTO replies(review_id,status,template_key,lang,stars,period,message_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (review_id, status, template_key, lang, stars, period, mh, datetime.utcnow().isoformat()+"Z"))
    conn.commit()

def choose_lang(lang: Optional[str]) -> str:
    if not lang:
        return "IT"
    code = lang.lower()
    if code.startswith("it"):
        return "IT"
    if code.startswith("en"):
        return "EN"
    if code.startswith("fr"):
        return "FR"
    return "IT"

def template_for(stars: int, period: str, lang: str) -> Optional[str]:
    key = f"{stars}_{period}_{lang}"
    return TEMPLATES.get(key)

async def post_reply(review_id: str, message: str):
    if not TP_BUSINESS_TOKEN:
        raise RuntimeError("TP_BUSINESS_TOKEN missing. Set it in environment.")
    url = f"{TP_API_BASE}/v1/private/reviews/{review_id}/reply"
    headers = {
        "Authorization": f"Bearer {TP_BUSINESS_TOKEN}",
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4())
    }
    payload = {"message": message, "replySource": "automation"}
    async with httpx.AsyncClient(timeout=20) as client:
        return await client.post(url, headers=headers, json=payload)

async def send_approval(review_id: str, message: str, stars: int, period: str, lang: str):
    if APP_APPROVAL_CHANNEL == "slack" and APP_APPROVAL_WEBHOOK:
        text = f"*Trustpilot review {review_id}*\nStars: {stars} | Period: {period} | Lang: {lang}\n\n*Proposed reply:*\n{message}\n\nApprove to post."
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(APP_APPROVAL_WEBHOOK, json={"text": text})

class ReviewEvent(BaseModel):
    review_id: str
    stars: int
    created_at: str
    language: Optional[str] = "it"
    consumer_name: Optional[str] = None
    company_response_exists: Optional[bool] = False

from fastapi import Request

@app.post("/webhook/trustpilot")
async def handle_trustpilot_event(event: ReviewEvent):
    if already_replied(event.review_id):
        return {"status": "skip", "reason": "already_replied"}

    if event.company_response_exists:
        save_log(event.review_id, "skip_company_already_replied", "", "", event.stars, "", "")
        return {"status": "skip", "reason": "company_already_replied"}

    lang = choose_lang(event.language)
    age = local_age_days(event.created_at)
    period = period_from_age(age)

    tpl = template_for(event.stars, period, lang)
    if not tpl:
        save_log(event.review_id, "skip_template_missing", "", lang, event.stars, period, "")
        await alert_error("Missing template", f"key={event.stars}_{period}_{lang}")
        raise HTTPException(status_code=400, detail=f"No template for {event.stars}_{period}_{lang}")

    name = event.consumer_name or "Cliente"
    message = tpl.replace("{name}", name)

    segment_critico = event.stars <= 2
    if APP_APPROVAL_MODE and segment_critico and period == "Fresco":
        await send_approval(event.review_id, message, event.stars, period, lang)
        save_log(event.review_id, "queued_for_approval", f"{event.stars}_{period}_{lang}", lang, event.stars, period, message)
        return {"status": "queued_for_approval"}

    try:
        resp = await post_reply(event.review_id, message)
        if resp.status_code in (200, 201):
            save_log(event.review_id, "replied", f"{event.stars}_{period}_{lang}", lang, event.stars, period, message)
            return {"status": "replied"}
        elif resp.status_code == 409:
            save_log(event.review_id, "skip_conflict", f"{event.stars}_{period}_{lang}", lang, event.stars, period, message)
            return {"status": "skip", "reason": "conflict"}
        else:
            save_log(event.review_id, f"error_{resp.status_code}", f"{event.stars}_{period}_{lang}", lang, event.stars, period, message)
            await alert_error("Trustpilot reply error", f"review_id={event.review_id} status={resp.status_code} body={resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except Exception as e:
        save_log(event.review_id, "error_exception", f"{event.stars}_{period}_{lang}", lang, event.stars, period, message)
        await alert_error("Exception while replying", f"review_id={event.review_id} error={str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
