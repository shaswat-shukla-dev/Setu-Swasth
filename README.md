# Setu Swasth (सेतु स्वस्थ)
### Persistent patient memory for India's fragmented rural healthcare — built on Cognee + Claude

> *Setu* (सेतु) — "bridge." *Swasth* (स्वस्थ) — "healthy."
> A bridge between every provider a patient sees — one memory that never resets.

Built for the **Cognee Memory Hackathon**.

---

## How this maps to the judging criteria

| Criterion | Where to look |
|---|---|
| **Potential Impact** | [The problem](#the-problem-india-specific) — a real, documented gap in India's ASHA → PHC → district-hospital → clinic chain, not a hypothetical use case. |
| **Creativity & Innovation** | The "⚠ Cross-visit conflict check" quick-ask button in the demo — it asks Cognee to reason *across* visits logged by different, unconnected providers (e.g. catching a prescription that conflicts with an allergy noted at a different facility weeks earlier), which a flat database/search can't do without custom logic. |
| **Technical Excellence** | Real (not simulated) OTP delivery with HMAC-hashed storage, constant-time comparison, and rate limiting — see [Consent & access control](#consent--access-control-dpdp-act-alignment). Runs Docker-first with health checks, and degrades gracefully to an in-memory fallback engine with no LLM key. |
| **Best Use of Cognee** | Every patient gets a genuinely isolated Cognee dataset (`patient_<id>`) — `remember`, `recall`, `improve`, and `forget` all consistently scope to it, so one patient's history can never leak into another's, and `forget()` actually erases real data. See [Cognee integration](#cognee-integration-kept-explicit-end-to-end) below. |
| **User Experience** | The "Patient consent required" lock walks a judge through the DPDP-style OTP flow in seconds; the "Memory Lens" panel shows every Cognee call live, so the memory layer is never a black box during a demo. |
| **Presentation Quality** | This README documents the problem, architecture, security model, and API end to end — see the table of contents below for a full walkthrough. |

---

## The problem (India-specific)

A patient in rural India routinely moves through several *disconnected*
layers of care:

1. An **ASHA worker** does a home visit and notes symptoms — usually on paper, if at all.
2. A **PHC (Primary Health Centre) doctor** sees the patient days later with no record of that visit.
3. A **district hospital** admits the patient with no visibility into the PHC's diagnosis, tests already run, or medicines already tried.
4. A **pharmacy or private clinic** repeats the cycle.

Nobody in this chain sees the full picture. Elderly or illiterate patients
often cannot accurately recount their own medical history. A drug allergy
noted at one clinic is invisible to the next, which is a **real patient
safety risk**, not a hypothetical one.

## The solution

Setu Swasth gives every patient a single, permanent, patient-owned memory
record — built directly on **Cognee's hybrid graph + vector memory engine** —
that any authorized provider can write to and read from, regardless of which
facility they work at.

- Every visit is **remembered** permanently under the patient's own ID.
- Any provider can **recall** the full history in plain language in seconds.
- Allergies and past diagnoses surface automatically — never left to a
  patient's memory or a lost paper slip.
- The record can be **improved** (memify) as it grows, and **forgotten**
  (corrected/erased) on request — memory here is a deliberate choice, not an
  uncontrolled data leak.
- Access requires the **patient's consent**, verified via an OTP flow, before
  any provider can view or add to the record — modeled on India's DPDP Act.

This maps 1:1 onto Cognee's own memory lifecycle:

```python
await cognee.remember(text, dataset_name=f"patient_{pid}", session_id=pid)   # log a visit
await cognee.recall(query, datasets=[f"patient_{pid}"], session_id=pid)      # ask about history
await cognee.improve(dataset=f"patient_{pid}")                               # a.k.a. memify
await cognee.forget(dataset=f"patient_{pid}")                                # correct / erase
```

**This is visible on the frontend, live** — the demo's "Memory Lens" panel
streams every one of these Cognee calls as they happen, so it's never a
black box: you can literally watch `remember()` fire when a visit is logged
and `recall()` fire when a provider asks a question.

## Architecture

```
mnemos/
├── backend/
│   ├── main.py            # FastAPI: patients, visits, consent/OTP, ask/recall, memory log
│   └── requirements.txt
├── frontend/
│   └── index.html         # single-file animated UI: consent flow, patient lookup, visit form, live Memory Lens
├── Dockerfile
├── docker-compose.yml
├── Procfile                # for Render/Railway/Heroku-style deploys
├── render.yaml              # one-click Render blueprint
├── .env.example
└── README.md
```

### Cognee integration (kept explicit, end to end)

- Each **patient** = one genuinely isolated Cognee dataset (`patient_<id>`,
  keyed by their patient ID — e.g. an Ayushman Bharat Health ID or an
  auto-generated `SS-XXXXXXXX`), with `session_id` also set to the patient
  ID for fast session-cache reads. Every one of the four calls below passes
  the dataset consistently — not just a session tag — so one patient's
  history can never bridge into another's permanent graph, and `forget()`
  targets data that was actually written.
- `POST /api/patient/{id}/visit` → `cognee.remember(text, dataset_name=..., session_id=id)`
- `POST /api/patient/{id}/ask` → `cognee.recall(query, datasets=[...], session_id=id)` → grounded Claude summary
  — the demo's "⚠ Cross-visit conflict check" button uses this to ask Cognee
  to reason across every visit logged by *different, unconnected providers*
  (e.g. catching a prescription that conflicts with an allergy noted at a
  different facility weeks earlier) — the kind of cross-record inference a
  flat keyword search can't do without hand-written rules.
- `POST /api/patient/{id}/improve` → `cognee.improve(dataset=...)`
- `POST /api/patient/{id}/forget` → `cognee.forget(dataset=...)`
- `GET /api/memory-log` → live feed of every Cognee call made, powering the
  frontend's Memory Lens panel

If no `LLM_API_KEY` is configured, the backend transparently swaps in a
lightweight in-memory engine with the **exact same interface**, so the whole
product — patient registration, visit logging, recall, the live memory feed —
is instantly demoable with zero setup, and upgrades itself automatically the
moment a real key is added. This is a demo-reliability choice, not a
shortcut around Cognee: the real integration is complete and unmodified from
Cognee's documented API.

### Claude integration

Claude (`claude-sonnet-4-6`, via the Anthropic Messages API) is used **only**
to turn what Cognee recalls into a clear, clinician-usable answer. It is
explicitly instructed to never invent a symptom, diagnosis, allergy, or
medicine that isn't present in the recalled memory — and to say so plainly
if the records don't answer the question.

### Built with Cognee's Claude Code integration

This project was developed using Cognee's official Claude Code integration
(persistent project memory for Claude Code itself while building the repo):
https://github.com/topoteretes/cognee-integrations/tree/main/integrations/claude-code

---

## Consent & access control (DPDP Act alignment)

Real patient health data in India must be handled under the **Digital
Personal Data Protection (DPDP) Act, 2023** — informed consent before a
provider accesses a patient's record. This is modeled, not just described:

1. A provider calls `POST /api/patient/{id}/request-access` with their name.
   A cryptographically random 6-digit OTP is generated (via Python's
   `secrets` module) and **emailed to the patient in real time** over SMTP
   if the patient has an email on file and SMTP is configured (see
   [Real-time OTP delivery](#real-time-otp-delivery-free) below). If not,
   the app automatically falls back to on-screen "demo OTP" mode — clearly
   labeled — so it still runs end-to-end without any external account.
2. The provider calls `POST /api/patient/{id}/verify-access` with that OTP.
   On success, they receive a **time-limited access token** (20 minutes).
3. Every sensitive endpoint — `visit`, `timeline`, `ask`, `forget` — requires
   a valid `X-Access-Token` header tied to that patient, or it returns
   `403 Forbidden`.
4. Every access request, grant, failed attempt, and erasure is written to
   an auditable trail: `GET /api/patient/{id}/consent-log`.

**Security hardening applied to the OTP flow:**
- OTPs are never stored in plaintext — only an HMAC-SHA256 digest (keyed by
  `OTP_SECRET`), so a memory dump or log leak can't reveal a live code.
- OTP comparison uses `hmac.compare_digest` (constant-time) to resist
  timing attacks.
- Verification is capped at 5 wrong attempts per OTP before it's invalidated.
- OTP requests are rate-limited to 3 per patient per rolling 10 minutes to
  block spam/abuse.
- OTPs expire after 5 minutes; access tokens expire after 20 minutes.

This is visible end-to-end in the frontend's demo panel: the "Patient
consent required" lock banner walks through requesting and entering the OTP
before any history becomes visible or editable.

**Production note**: a real deployment would also want role-based field
visibility (e.g. an ASHA worker may not need to see full hospital records)
and persistent (not in-memory) storage for OTP/rate-limit state across
restarts — this implementation demonstrates the access-control *pattern*
end to end, now backed by genuine email delivery instead of a simulated one.

### Real-time OTP delivery (free)

No SMS gateway needed — OTPs are delivered over **email**, which is free at
real-world volumes and needs no billing account. Set these in `.env`:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=<16-character Gmail App Password>
SMTP_FROM=you@gmail.com
OTP_SECRET=<run: python -c "import secrets; print(secrets.token_hex(32))">
```

**Gmail (easiest, 500 emails/day free):**
1. Turn on 2-Step Verification at `myaccount.google.com/security`.
2. Create an App Password at `myaccount.google.com/apppasswords`.
3. Use that 16-character password as `SMTP_PASS` — not your normal Gmail password.

**Brevo or Resend (better deliverability for a real deployment, still free,
no credit card, ~300 emails/day):** sign up, grab your SMTP credentials, and
set `SMTP_HOST=smtp-relay.brevo.com` (or Resend's SMTP host) with the
provided user/key.

Once configured, register a patient with an email address and every OTP is
delivered live to their inbox — `GET /api/health` will report
`"otp_delivery": "email (live)"` to confirm it's active.

---

## Quickstart (Docker — recommended)

```bash
cd setu-swasth
cp .env.example .env        # add LLM_API_KEY, and SMTP_* for real email OTP (optional)
docker compose up --build
```

Open **http://localhost:8000** — the backend serves the frontend directly.
A demo patient (Kamala Devi, with a realistic 3-provider visit history) is
seeded automatically so the product is explorable immediately.

Without `SMTP_*` configured, OTPs show on-screen (demo mode). With them
configured, OTPs are emailed to the patient in real time — see
[Real-time OTP delivery](#real-time-otp-delivery-free) above for free setup
options (Gmail App Password, Brevo, Resend).

## Quickstart (local, no Docker)

```bash
cd setu-swasth
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env        # add LLM_API_KEY + SMTP_* as above
cd backend
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000**.

## Enabling full Cognee memory + Claude answers

1. Get an Anthropic API key: https://console.anthropic.com/
2. Put it in `.env`:
   ```
   LLM_API_KEY=sk-ant-...
   ```
3. Restart the backend. `/api/health` reports `"engine": "cognee"` once
   active, and the status pill in the UI turns green with the live engine name.

To point at **Cognee Cloud** or a remote instance instead of an embedded
engine, set `COGNEE_BASE_URL` and `COGNEE_API_KEY` — see
[Cognee's docs](https://docs.cognee.ai) for `cognee.serve(...)`.

## API reference

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/api/health` | — | Reports active memory engine |
| POST | `/api/patients` | `{name, age?, gender?, village?, phone?, email?, patient_id?}` | Register a patient → opens a Cognee dataset (`email` enables real-time OTP delivery) |
| GET | `/api/patients` | — | List all registered patients |
| GET | `/api/patient/{id}` | — | Patient summary + visit count |
| POST | `/api/patient/{id}/request-access` | `{provider_name, provider_type?}` | Send consent OTP to patient's email in real time (falls back to on-screen demo OTP if no email/SMTP configured) |
| POST | `/api/patient/{id}/verify-access` | `{otp, provider_name}` | Verify OTP → issue a 20-min `access_token` |
| GET | `/api/patient/{id}/consent-log` | — | Audit trail of access requests/grants/erasures |
| POST | `/api/patient/{id}/visit` *(requires `X-Access-Token`)* | `{provider_name, provider_type, facility?, symptoms?, diagnosis?, prescription?, allergies_noted?, notes?}` | Log a visit → `cognee.remember()` |
| GET | `/api/patient/{id}/timeline` *(requires `X-Access-Token`)* | — | Chronological visit list (for the UI timeline) |
| POST | `/api/patient/{id}/ask` *(requires `X-Access-Token`)* | `{question, asked_by?}` | Ask about history → `cognee.recall()` → Claude synthesis |
| POST | `/api/patient/{id}/improve` | — | Trigger `cognee.improve()` (memify) |
| POST | `/api/patient/{id}/forget` *(requires `X-Access-Token`)* | `{reason?}` | Erase this patient's memory → `cognee.forget()` |
| GET | `/api/memory-log?limit=` | — | Live feed of recent Cognee calls (Memory Lens) |

## Deployment

Setu Swasth ships as a single Docker image — deploy it anywhere Docker (or
a Docker-native PaaS) runs.

**Render (one-click blueprint included):**
```bash
# push this repo to GitHub, then in Render:
# New -> Blueprint -> select this repo -> it reads render.yaml automatically
# (env: docker, builds from ./Dockerfile)
# set these in the Render dashboard's environment variables:
#   LLM_API_KEY   (required for Cognee + Claude synthesis)
#   SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM, OTP_SECRET   (for real email OTP)
```

**Railway / Fly.io:**
```bash
# push the repo; both platforms auto-detect the Dockerfile
# set LLM_API_KEY + SMTP_* + OTP_SECRET as environment variables
# expose port 8000 (or let the platform inject $PORT — the Dockerfile respects it)
```

**Any VPS:**
```bash
cp .env.example .env   # fill in LLM_API_KEY + SMTP_* + OTP_SECRET
docker compose up -d --build
```

The included `docker-compose.yml` adds a health check and `restart:
unless-stopped`, so it comes back up on its own after a host reboot or crash.

**Cognee Cloud:** for managed memory infrastructure instead of running Cognee
embedded, set `COGNEE_BASE_URL` + `COGNEE_API_KEY`.

## Naming note

"Setu Swasth" was chosen by the project owner. A web search during
development found existing prior use of similar names in India's health-tech
space (e.g. an existing "Swasthya Setu" app, and "Setu" as an established
fintech brand) — this is noted here for transparency. If this project is
taken beyond a hackathon submission, a trademark and domain search (via a
registrar and India's IP India database) is strongly recommended before
committing to the name commercially.

## Data & privacy note

This is a hackathon prototype. For real deployment in India, patient health
data would need to comply with the **Digital Personal Data Protection (DPDP)
Act, 2023** — informed consent for data collection, purpose limitation, and
a patient's right to erasure. The consent/OTP flow and `forget()` endpoint
are first steps toward that, and OTP delivery is now real (email, hashed at
rest, rate-limited) rather than simulated — but a production system would
still want persistent session storage, an optional SMS channel for
patients without reliable email/data access, and role-based field-level
access control before handling real patient data at scale.

## License

MIT — build on it freely.
