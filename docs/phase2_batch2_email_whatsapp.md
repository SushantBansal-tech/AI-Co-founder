# Phase 2, Batch 2: Email and WhatsApp adapters

## Runtime flow

Both adapters normalize provider input into `IncomingInquiry` and insert a
durable `channel_inbound_jobs` row. The webhook or mailbox poller does not run
the sales graph directly. A background worker claims pending jobs, runs the
shared channel ingestion service, and persists the resulting ingestion and
LangGraph thread IDs.

The worker uses PostgreSQL `FOR UPDATE SKIP LOCKED`, so multiple application
workers can safely compete for pending jobs. Failed jobs use exponential
backoff and become terminal after five attempts. Jobs left in `processing`
after a process crash are returned to `pending` when a worker starts.

## Apply the migration

```powershell
alembic upgrade head
alembic current
```

The expected revision is `fa729bd351e8`.

## Configure email

Store credentials in environment variables; only their variable names are
stored in the database.

```powershell
$env:SALES_EMAIL_USERNAME = "sales@example.com"
$env:SALES_EMAIL_PASSWORD = "<mailbox-app-password>"
$env:EMAIL_POLLER_ENABLED = "true"
$env:EMAIL_POLL_INTERVAL_SECONDS = "30"

python scripts/create_email_source.py `
  --business-id "demo-steel-company" `
  --public-key "sales-email" `
  --mailbox "sales@example.com" `
  --imap-host "imap.example.com" `
  --smtp-host "smtp.example.com"
```

The poller uses stable IMAP UIDs and persists both UIDVALIDITY and the last
processed UID in `channel_cursors`. It uses `BODY.PEEK[]`, so reading a message
does not mark it as read.

## Configure Meta WhatsApp Cloud API

```powershell
$env:WHATSAPP_VERIFY_TOKEN = "<random-verify-token>"
$env:WHATSAPP_APP_SECRET = "<meta-app-secret>"
$env:WHATSAPP_ACCESS_TOKEN = "<system-user-access-token>"

python scripts/create_whatsapp_source.py `
  --business-id "demo-steel-company" `
  --public-key "sales-whatsapp" `
  --phone-number-id "<meta-phone-number-id>"
```

Configure this public HTTPS callback in Meta:

```text
https://<your-public-host>/channels/whatsapp/sales-whatsapp/webhook
```

For local webhook testing, expose Uvicorn through a trusted HTTPS tunnel. The
GET callback verifies the configured token. Every POST callback verifies
`X-Hub-Signature-256` against the raw request body before parsing JSON.

## Start and test

```powershell
uvicorn app.main:app --reload
pytest tests/phase2 -q
```

`CHANNEL_WORKER_ENABLED` defaults to `true`. Set it to `false` only when a
separate worker process owns inbound jobs.

## Operational notes

- Keep Meta app secrets, access tokens, and mailbox passwords out of
  `channel_sources.configuration`.
- A provider event ID is unique per tenant/channel/provider, so provider
  retries do not create duplicate leads.
- WhatsApp phone-number metadata must match the configured source account;
  mismatched payloads are not routed to another tenant.
- Email attachments and WhatsApp media are represented as attachment
  references. Binary download, malware scanning, and durable object storage
  should be enabled before agents are allowed to read attachment bodies.
