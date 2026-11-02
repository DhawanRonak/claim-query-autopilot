# Claim Query Autopilot

Auto-reads health-insurance query letters and gets the missing documents from patients over WhatsApp and email.
Originally built as a product case study for [care.fi](https://care.fi).

A DigiLocker-style portal for patients' claim documents. When an insurer's query letter lands in a
patient's locker, the system reads it, checks the locker, and messages the patient on WhatsApp and
email with exactly what's still needed. When the patient replies on WhatsApp, their files go
straight into the locker, the pending list updates, and they get an automatic reply. No one clicks anything.

```
query letter added ─► split into points ─► check locker ─► fill template ─► WhatsApp + email
   (portal / ZIP import / inbox folder / API)   (deterministic)                   │
                                                                                   ▼
        query marked complete ◄── pending list updates ◄── file classified ◄── patient replies on WhatsApp
```

## What it does

| Area | Features |
|---|---|
| Patient locker | Profile, documents, query letters, message history (in and out), core-document checklist, live "pending from patient" list |
| Query letters | AI reads PDFs, scans and photos; splits every point; maps each to one of 20 document types; rules-engine fallback if the AI is unavailable |
| Decisions | Deterministic locker lookup: never asks for what we have, routes hospital-held papers to the hospital team, re-asks when the insurer rejected a copy, flags anything unclear for review |
| Messages | Fixed template (`templates_msg/`), plain language, a "why" per item, real deadline date, both channels in parallel, identical messages never sent twice, reminder before the deadline |
| WhatsApp replies | Files saved to the right patient's locker and classified; duplicates spotted; blurry or other-patient documents flagged; typed explanations accepted; auto-reply with what's still pending; query auto-completes |
| Documents | Classified from their contents (not file name); confidence shown; agent can correct the type in one click; multi-file upload |
| Agent tools | Dashboard with KPIs and a "needs attention" queue, search, override any point's decision, resend, send reminder, re-read letter |
| Admin | Create/edit patients, bulk import a ZIP of profiles + documents + letters, settings page with test send, access log |
| Patient data | Role-based access (admin / agent sees only assigned patients / patient sees only self), masked contacts, private file storage, CSRF protection, login rate limit, Twilio signature check, no diagnosis in messages, full audit trail |

## Run it locally

```bash
cp .env.example .env      # add keys (below); works with none, in simulated mode
./run.sh                  # Mac/Linux; on Windows double-click run.bat
                          # installs, seeds 10 demo patients, starts on http://localhost:8000
python tests/test_flows.py   # 36 end-to-end checks, never sends real messages
```

| Login | Password | Sees |
|---|---|---|
| `admin` | `CareFi@2026` | everyone, import, settings, access log |
| `agent.priya` | `Agent@2026` | patients CF-P001 to P005 only |
| `agent.rahul` | `Agent@2026` | patients CF-P006 to P010 only |
| `p001` … `p010` | `Patient@2026` | their own locker only |

## Keys (all in `.env`)

1. **Gemini** (free): aistudio.google.com/apikey → `GEMINI_API_KEY`. Needed for scanned letters and
   photos; Claude or OpenAI keys also work.
2. **Twilio WhatsApp sandbox**: Account SID + Auth Token. Each receiving phone sends the join code
   (e.g. `join xyz-abc`) to +1 415 523 8886 once.
3. **Gmail App Password**: `SMTP_USER` + `SMTP_PASSWORD` (needs 2-step verification).
4. **Demo routing**: `WHATSAPP_OVERRIDE_TO` / `EMAIL_OVERRIDE_TO` = care.fi's test phone and email.
5. **Public URL**: `PUBLIC_BASE_URL`, then paste `<url>/webhooks/twilio/whatsapp` into the Twilio
   sandbox's "When a message comes in" field (Settings page shows the exact URL).

Admin → Settings → "Send test" confirms both channels work.

## Put it online

- **Quickest**: `ngrok http 8000`, set `PUBLIC_BASE_URL` to the ngrok URL, restart.
- **Render**: push this folder to GitHub, New → Blueprint (uses `render.yaml`, Docker, 1 GB disk for data),
  fill in the secret env vars.

## Ways a query letter enters the locker (all start processing instantly)
1. Upload on the patient's page.  2. Admin → Import (ZIP with a `queries/` folder).
3. Drop into `inbox/<PATIENT_ID>/`.  4. `curl -H "x-api-key: $INBOUND_API_KEY" -F file=@letter.pdf <url>/api/patients/CF-P001/queries`

## Import format (Admin → Import)
```
bundle.zip
├── patients.csv            (or .xlsx / .json; column names matched loosely)
├── CF-P001/ discharge.pdf, aadhaar.jpg …   (folder per patient ID or name)
├── CF-P002_bill.pdf        (or the ID as a file prefix)
└── queries/ CF-P001_letter.pdf
```

## How it decides

The AI only reads; the decision is a lookup in `app/taxonomy.py` (20 document types, each held by the patient or the hospital).

| Point needs… | Locker has it? | Decision |
|---|---|---|
| any document | yes | Already in locker: not asked, patient told "we already have it" |
| patient-held document | no | Ask patient |
| hospital-held document (ICP, OT notes, implant invoice…) | no | Hospital team, never sent to the patient |
| patient-held document the insurer called illegible / unsigned | yes | Ask patient for a fresh copy |
| an explanation (reason for delay, habits) | n/a | Ask patient for a short signed note (a typed WhatsApp reply counts) |
| nothing recognisable | n/a | Agent review |

## WhatsApp via Meta Cloud API (free, preferred)
Set `META_WA_TOKEN`, `META_WA_PHONE_NUMBER_ID`, `META_VERIFY_TOKEN` (any string) and `META_APP_SECRET`.
Webhook: `<url>/webhooks/meta`, subscribe to **messages**. WhatsApp only allows free-text messages within
24 hours of the patient last messaging the business number, so on the test number each recipient sends "hi" first.

## Known limits
- Twilio's free trial sends only Twilio's fixed sample templates; use Meta (free) or an upgraded Twilio account.
- A single PDF containing several different documents is classified as one document.
- Business-initiated WhatsApp outside the 24-hour window needs an approved template in production.
