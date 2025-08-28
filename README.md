# Trustpilot Auto-Reply Bot (FastAPI)

Starter per risposte automatiche con template multilingua (IT/EN/FR) e log su SQLite.

## Setup
1. Python 3.11+
2. `pip install -r requirements.txt`
3. Copia `.env.example` in `.env` e compila i valori (token Trustpilot, webhook Slack se usi l'approvazione).
4. Avvia:
```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

## Webhook
Collega `POST /webhook/trustpilot` dal pannello Trustpilot. Payload atteso:
```json
{
  "review_id": "abcd1234",
  "stars": 2,
  "created_at": "2025-08-27T10:20:30Z",
  "language": "it",
  "consumer_name": "Mario Rossi",
  "company_response_exists": false
}
```

## Log & Idempotenza
- DB `bot.sqlite3` con tabella `replies`.
- Dedup su `review_id`.
- `Idempotency-Key` nella POST verso Trustpilot.

## Approvazione 1–2★ fresche
- `APP_APPROVAL_MODE=true` per accodare bozze a Slack (o disattiva per full auto).
- Se vecchie (>5g), l’app risponde direttamente in modo empatico e invita all’aggiornamento.

## Template
- In `templates.json` chiavi `{stars}_{period}_{lang}`.
- Placeholder: `{name}`.

## Produzione
- Metti dietro proxy HTTPS, aggiungi secret/firma webhook, ruota token OAuth, monitora errori.


## Alert sugli errori
"
- Configura `.env`:
"
  - `ALERT_CHANNEL=slack|email|both|none`
"
  - Se Slack: `ALERT_SLACK_WEBHOOK`
"
  - Se Email: `ALERT_EMAIL_TO`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_TLS`
"
- Il bot invia un alert quando:
"
  - manca un template per la chiave calcolata
"
  - la POST verso Trustpilot risponde con codice non 2xx/409
"
  - avviene un'eccezione runtime durante la pubblicazione
"
