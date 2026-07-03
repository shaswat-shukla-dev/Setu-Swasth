"""
Setu Swasth (सेतु स्वस्थ) — Persistent Patient Memory for
Fragmented Rural Healthcare in India.

THE PROBLEM (India-specific):
    A patient in rural India routinely sees a PHC doctor, then an ASHA worker
    on a home visit, then a district hospital, then a private clinic in
    town — each with its own paper slip or nothing at all. No one sees the
    full picture: what was prescribed last time, what allergy nearly caused
    a reaction, what the diagnosis trend looks like over 6 visits. Patients,
    especially elderly or illiterate patients, often cannot recount their
    own history accurately. This is a documented, chronic gap in India's
    primary healthcare system (ASHA / PHC / CHC / district hospital chain).

THE SOLUTION:
    Every visit — regardless of which provider or facility — is written into
    one permanent, patient-owned memory using Cognee's hybrid graph+vector
    engine. Any future provider (with the patient's consent, via patient ID)
    can instantly recall the FULL history: symptoms, diagnoses, prescriptions,
    allergies, and provider notes — synthesized into a clear answer by Claude,
    grounded only in what was actually recorded.

COGNEE USAGE (this is the core of the project — kept explicit end to end):
    Each patient's record lives in its own Cognee dataset ("patient_<id>"),
    with session_id also set to the patient ID for fast session-cache reads:
        await cognee.remember(text, dataset_name=f"patient_{pid}", session_id=pid)
        await cognee.recall(query, datasets=[f"patient_{pid}"], session_id=pid)
        await cognee.improve(dataset=f"patient_{pid}")            # periodic memify
        await cognee.forget(dataset=f"patient_{pid}")              # correct/erase a record
    Passing dataset_name/datasets/dataset consistently on every call (not
    just session_id) is what actually keeps one patient's history from
    bridging into another's permanent graph, and what makes forget() target
    real, previously-written data instead of a no-op.
    Every one of these calls is logged in MEMORY_LOG and exposed via
    GET /api/memory-log so the frontend can show, live, exactly when and how
    Cognee is being used — nothing is hidden behind a black box.

CLAUDE USAGE:
    Claude (claude-sonnet-4-6, via api.anthropic.com) is used ONLY to
    synthesize a readable clinical answer from what Cognee recalls — it is
    explicitly instructed never to invent facts not present in memory.

DEV-TOOLING NOTE:
    This backend was built with Cognee's official Claude Code integration
    (see https://github.com/topoteretes/cognee-integrations/tree/main/integrations/claude-code)
    which gives Claude Code itself persistent project memory across sessions
    while building this repo.
"""

import os
import re
import time
import uuid
import hmac
import hashlib
import secrets
import smtplib
import asyncio
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, EmailStr

load_dotenv()

APP_NAME = "Setu Swasth"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # Anthropic key — powers Cognee's graph AND Claude synthesis

# --------------------------------------------------------------------------
# Real-time OTP delivery (email — free, no SMS-gateway bill required)
#
# Any SMTP account works free of charge:
#   - Gmail: create an "App Password" at myaccount.google.com/apppasswords
#     (2-Step Verification must be on). 500 emails/day free, no billing.
#   - Brevo (formerly Sendinblue) / Resend: free transactional-email tier,
#     no credit card, ~300 emails/day — good for real deployments.
# Configure via .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM.
# If unset, the app automatically falls back to on-screen "demo OTP" mode
# so the product still runs end-to-end without any external account.
# --------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

# Secret key used to HMAC-hash OTPs before they ever touch memory/logs.
# Set OTP_SECRET in .env for production so tokens survive a restart;
# otherwise a random one is generated per process (fine for a single run).
OTP_SECRET = os.getenv("OTP_SECRET") or secrets.token_hex(32)

app = FastAPI(title=f"{APP_NAME} API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------
# Memory engine — real Cognee when an LLM key is configured, safe fallback
# otherwise, so the product always runs end-to-end for judges/demo.
# --------------------------------------------------------------------------

COGNEE_AVAILABLE = False
try:
    if LLM_API_KEY:
        import cognee  # noqa: F401
        COGNEE_AVAILABLE = True
except Exception:
    COGNEE_AVAILABLE = False

MEMORY_LOG: List[Dict[str, Any]] = []          # live feed of every Cognee call made
PATIENTS: Dict[str, Dict[str, Any]] = {}       # demo patient directory (fallback + display metadata)
VISITS: Dict[str, List[Dict[str, Any]]] = {}   # patient_id -> ordered visit records (for the timeline UI)

# --------------------------------------------------------------------------
# Patient consent / access control
#
# Real deployment note: India's DPDP Act, 2023 requires informed consent
# before a provider can access a patient's personal health data. Here that
# consent is modeled as an OTP emailed to the patient in real time over SMTP
# (falls back to an on-screen demo OTP if no email/SMTP is configured) — a
# provider cannot read or write a patient's history without the patient
# approving access for that session. Every access request/grant is logged
# and auditable via /api/patient/{id}/consent-log.
# --------------------------------------------------------------------------

OTP_TTL_SECONDS = 5 * 60          # OTP must be used within 5 minutes
ACCESS_TOKEN_TTL_SECONDS = 20 * 60  # granted access session lasts 20 minutes
OTP_MAX_ATTEMPTS = 5               # wrong-guess attempts allowed before an OTP is invalidated
OTP_MAX_REQUESTS_PER_WINDOW = 3    # how many OTPs a patient can have sent per window
OTP_REQUEST_WINDOW_SECONDS = 10 * 60  # ...within a rolling 10-minute window (anti-spam / anti-abuse)

CONSENT_OTP: Dict[str, Dict[str, Any]] = {}       # patient_id -> {otp_hash, expires, provider_name, attempts}
ACCESS_TOKENS: Dict[str, Dict[str, Any]] = {}     # token -> {patient_id, provider_name, expires}
CONSENT_LOG: Dict[str, List[Dict[str, Any]]] = {} # patient_id -> audit trail of access events
OTP_REQUEST_HISTORY: Dict[str, List[float]] = {}  # patient_id -> timestamps of recent OTP sends (rate limiting)


def _consent_log(pid: str, event: str, provider_name: str):
    CONSENT_LOG.setdefault(pid, []).append({
        "event": event, "provider_name": provider_name, "ts": time.time(),
    })


def _hash_otp(pid: str, otp: str) -> str:
    """OTPs are never stored in plaintext — only an HMAC-SHA256 digest,
    so a memory dump or log leak can't reveal live codes."""
    return hmac.new(OTP_SECRET.encode(), f"{pid}:{otp}".encode(), hashlib.sha256).hexdigest()


def _mask_email(email: str) -> str:
    try:
        local, domain = email.split("@", 1)
        keep = local[:2] if len(local) > 2 else local[:1]
        return f"{keep}{'*' * max(len(local) - len(keep), 2)}@{domain}"
    except Exception:
        return "your registered email"


def _check_rate_limit(pid: str):
    now = time.time()
    history = [t for t in OTP_REQUEST_HISTORY.get(pid, []) if now - t < OTP_REQUEST_WINDOW_SECONDS]
    if len(history) >= OTP_MAX_REQUESTS_PER_WINDOW:
        wait = int(OTP_REQUEST_WINDOW_SECONDS - (now - history[0]))
        raise HTTPException(429, f"Too many OTP requests. Try again in {max(wait, 1)} seconds.")
    history.append(now)
    OTP_REQUEST_HISTORY[pid] = history


def _issue_otp(pid: str, provider_name: str) -> str:
    _check_rate_limit(pid)
    # Cryptographically secure 6-digit code (not uuid/random-based).
    otp = f"{secrets.randbelow(1_000_000):06d}"
    CONSENT_OTP[pid] = {
        "otp_hash": _hash_otp(pid, otp),
        "expires": time.time() + OTP_TTL_SECONDS,
        "provider_name": provider_name,
        "attempts": 0,
    }
    _consent_log(pid, "otp_requested", provider_name)
    return otp


async def _send_otp_email(to_email: str, otp: str, patient_name: str, provider_name: str) -> bool:
    """Sends the OTP over real SMTP in a background thread (smtplib is
    blocking). Returns True on success so the caller can decide whether to
    fall back to on-screen demo mode."""
    if not SMTP_CONFIGURED:
        return False

    subject = f"Setu Swasth — Access code {otp}"
    body = (
        f"A healthcare provider ({provider_name}) is requesting consent to view or "
        f"add to {patient_name}'s Setu Swasth health record.\n\n"
        f"Your one-time access code is: {otp}\n\n"
        f"This code expires in {OTP_TTL_SECONDS // 60} minutes and can only be used once.\n"
        f"If you did not expect this request, do not share this code with anyone and "
        f"you may safely ignore this email — no access will be granted without it.\n\n"
        f"— Setu Swasth (सेतु स्वस्थ)"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    def _send():
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())

    try:
        await asyncio.to_thread(_send)
        return True
    except Exception:
        return False


def _verify_otp(pid: str, otp: str, provider_name: str) -> str:
    entry = CONSENT_OTP.get(pid)
    if not entry or time.time() > entry["expires"]:
        CONSENT_OTP.pop(pid, None)
        _consent_log(pid, "otp_verification_failed", provider_name)
        raise HTTPException(401, "Invalid or expired OTP. Request access again.")

    if entry["attempts"] >= OTP_MAX_ATTEMPTS:
        CONSENT_OTP.pop(pid, None)
        _consent_log(pid, "otp_locked_out", provider_name)
        raise HTTPException(429, "Too many incorrect attempts. Request a new OTP.")

    # Constant-time comparison — resists timing attacks on the hash.
    if not hmac.compare_digest(_hash_otp(pid, otp), entry["otp_hash"]):
        entry["attempts"] += 1
        _consent_log(pid, "otp_verification_failed", provider_name)
        remaining = OTP_MAX_ATTEMPTS - entry["attempts"]
        raise HTTPException(401, f"Incorrect OTP. {max(remaining, 0)} attempt(s) remaining.")

    token = secrets.token_hex(32)
    ACCESS_TOKENS[token] = {"patient_id": pid, "provider_name": provider_name, "expires": time.time() + ACCESS_TOKEN_TTL_SECONDS}
    CONSENT_OTP.pop(pid, None)
    _consent_log(pid, "access_granted", provider_name)
    return token


def _require_access(pid: str, token: Optional[str]):
    if not token:
        raise HTTPException(403, "Patient consent required. Request an access OTP first.")
    entry = ACCESS_TOKENS.get(token)
    if not entry or entry["patient_id"] != pid or time.time() > entry["expires"]:
        raise HTTPException(403, "Access session expired or invalid. Request a new OTP.")
    return entry


def _log(op: str, patient_id: str, detail: str):
    MEMORY_LOG.append({
        "id": str(uuid.uuid4()),
        "op": op,                 # remember | recall | improve | forget
        "patient_id": patient_id,
        "detail": detail[:180],
        "engine": "cognee" if COGNEE_AVAILABLE else "fallback-in-memory",
        "ts": time.time(),
    })
    if len(MEMORY_LOG) > 300:
        del MEMORY_LOG[: len(MEMORY_LOG) - 300]


class FallbackMemory:
    """Drop-in memory engine with the same shape as Cognee's, used when no
    LLM key is configured. Keeps the whole product demoable offline."""

    def __init__(self):
        self.store: Dict[str, List[Dict[str, Any]]] = {}

    async def remember(self, text: str, session_id: Optional[str] = None, **_):
        bucket = session_id or "unassigned"
        self.store.setdefault(bucket, []).append({"id": str(uuid.uuid4()), "text": text, "ts": time.time()})
        await asyncio.sleep(0)
        return {"status": "stored", "dataset": bucket}

    async def recall(self, query: str, session_id: Optional[str] = None, **_):
        bucket = session_id or "unassigned"
        pool = self.store.get(bucket, [])
        q = set(query.lower().split())
        scored = []
        for item in pool:
            words = set(item["text"].lower().split())
            score = len(q & words)
            if score > 0 or len(pool) <= 6:
                scored.append((score, item))
        scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))
        results = [s[1]["text"] for s in scored[:8]]
        await asyncio.sleep(0)
        return results or ["No prior records found for this patient yet."]

    async def improve(self, **_):
        await asyncio.sleep(0)
        return {"status": "memory refined"}

    async def forget(self, dataset: str = "unassigned", **_):
        self.store.pop(dataset, None)
        await asyncio.sleep(0)
        return {"status": "forgotten", "dataset": dataset}


_fallback = FallbackMemory()


def _dataset_name(patient_id: str) -> str:
    """Cognee dataset names should be simple identifiers. Every patient gets
    their own dataset so remember/recall/improve/forget all stay correctly
    isolated per patient in the *permanent* graph — not just in the
    short-lived session cache, which was the bug in the previous version
    (session_id alone does not partition the permanent graph; without an
    explicit per-patient dataset, remember() bridges everyone's records into
    the shared "main_dataset", and forget(dataset=patient_id) was a no-op
    because nothing was ever stored under that dataset name)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", patient_id)
    return f"patient_{safe}"


async def mem_remember(text: str, patient_id: str):
    """Cognee call #1: await cognee.remember(text, dataset_name=..., session_id=patient_id)
    dataset_name gives this patient their own permanent-graph partition;
    session_id keeps the fast session-cache path for immediate recall."""
    _log("remember", patient_id, text)
    if COGNEE_AVAILABLE:
        import cognee
        return await cognee.remember(text, dataset_name=_dataset_name(patient_id), session_id=patient_id)
    return await _fallback.remember(text, session_id=patient_id)


async def mem_recall(query: str, patient_id: str):
    """Cognee call #2: await cognee.recall(query, datasets=[...], session_id=patient_id)
    Restricting to this patient's dataset (in addition to the session cache)
    guarantees one patient's history is never mixed into another's answer."""
    _log("recall", patient_id, query)
    if COGNEE_AVAILABLE:
        import cognee
        results = await cognee.recall(query, datasets=[_dataset_name(patient_id)], session_id=patient_id)
        return [_extract_recall_text(r) for r in results] if results else []
    return await _fallback.recall(query, session_id=patient_id)


def _extract_recall_text(entry: Any) -> str:
    """Cognee's recall() returns typed response entries (graph text chunks,
    QA cache hits, session context, etc.) rather than plain strings — pull
    out the field that actually holds the human-readable content instead of
    falling back to a raw repr()."""
    for attr in ("text", "answer", "content"):
        val = getattr(entry, attr, None)
        if val:
            return str(val)
    return str(entry)


async def mem_improve(patient_id: str = "global"):
    """Cognee call #3: await cognee.improve(dataset=...)  (a.k.a memify)
    Scoped to a single patient's dataset — never runs against the whole
    memory layer, so improving one patient's graph can't leak into another's."""
    _log("improve", patient_id, "post-ingestion enrichment / prune stale nodes")
    if COGNEE_AVAILABLE:
        import cognee
        return await cognee.improve(dataset=_dataset_name(patient_id))
    return await _fallback.improve()


async def mem_forget(patient_id: str):
    """Cognee call #4: await cognee.forget(dataset=...)
    Targets the same dataset name remember() actually wrote to, so this now
    genuinely erases the patient's permanent-graph data (previously it
    pointed at a dataset that was never created, so it silently did nothing)."""
    _log("forget", patient_id, "dataset erased on request")
    if COGNEE_AVAILABLE:
        import cognee
        return await cognee.forget(dataset=_dataset_name(patient_id))
    return await _fallback.forget(dataset=patient_id)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class NewPatient(BaseModel):
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    village: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None  # OTP access codes are delivered here in real-time
    patient_id: Optional[str] = None  # e.g. existing Ayushman Bharat Health ID; auto-generated if absent


class VisitLog(BaseModel):
    provider_name: str
    provider_type: str = Field(description="Doctor | ASHA Worker | PHC Nurse | Pharmacist | District Hospital")
    facility: Optional[str] = None
    symptoms: Optional[str] = None
    diagnosis: Optional[str] = None
    prescription: Optional[str] = None
    allergies_noted: Optional[str] = None
    notes: Optional[str] = None


class AskRequest(BaseModel):
    question: str
    asked_by: Optional[str] = "Provider"


class ForgetRequest(BaseModel):
    reason: Optional[str] = "Correction requested"


class AccessRequest(BaseModel):
    provider_name: str
    provider_type: Optional[str] = "Provider"


class AccessVerify(BaseModel):
    otp: str
    provider_name: str


# --------------------------------------------------------------------------
# Patient directory
# --------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "app": APP_NAME,
        "status": "ok",
        "engine": "cognee" if COGNEE_AVAILABLE else "fallback-in-memory",
        "otp_delivery": "email (live)" if SMTP_CONFIGURED else "demo (on-screen)",
        "patients_registered": len(PATIENTS),
    }


@app.post("/api/patients")
async def create_patient(p: NewPatient):
    pid = p.patient_id or ("SS-" + uuid.uuid4().hex[:8].upper())
    if pid in PATIENTS:
        raise HTTPException(400, "patient_id already exists")
    PATIENTS[pid] = {
        "patient_id": pid, "name": p.name, "age": p.age, "gender": p.gender,
        "village": p.village, "phone": p.phone, "email": p.email, "created": time.time(),
    }
    VISITS[pid] = []
    intro = f"Patient record opened. Name: {p.name}. Age: {p.age}. Gender: {p.gender}. Village: {p.village}."
    await mem_remember(intro, patient_id=pid)
    return {"ok": True, "patient": PATIENTS[pid]}


@app.get("/api/patients")
async def list_patients():
    return {"ok": True, "patients": list(PATIENTS.values())}


@app.get("/api/patient/{pid}")
async def get_patient(pid: str):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    return {"ok": True, "patient": PATIENTS[pid], "visit_count": len(VISITS.get(pid, []))}


# --------------------------------------------------------------------------
# Consent / OTP access control
#
# Per India's DPDP Act, 2023, a provider must have the patient's informed
# consent before viewing or adding to their health record. Here, requesting
# access emails a real, cryptographically random 6-digit OTP to the
# patient's registered address (or shows it on screen in demo mode if no
# email/SMTP is configured); only submitting that OTP unlocks a
# time-limited access session for that provider.
# --------------------------------------------------------------------------

@app.post("/api/patient/{pid}/request-access")
async def request_access(pid: str, req: AccessRequest):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")

    patient = PATIENTS[pid]
    otp = _issue_otp(pid, req.provider_name)  # rate-limited + securely hashed internally

    email = patient.get("email")
    if email and SMTP_CONFIGURED:
        sent = await _send_otp_email(email, otp, patient.get("name", "the patient"), req.provider_name)
        if sent:
            return {
                "ok": True,
                "message": f"A real-time OTP was emailed to {_mask_email(email)}.",
                "delivery": "email",
                "expires_in_seconds": OTP_TTL_SECONDS,
            }
        # Email attempt failed (bad creds, network, etc.) — fall through to
        # demo mode below so the flow never dead-ends for the provider.

    # No email on file, or SMTP isn't configured, or delivery failed:
    # fall back to on-screen demo mode so the product still runs end-to-end.
    reason = "no email on file for this patient" if not email else "email delivery is not configured"
    return {
        "ok": True,
        "message": f"Demo mode ({reason}) — showing the code on screen instead of emailing it.",
        "delivery": "demo",
        # NOTE: in a live deployment with SMTP configured and a patient email
        # on file, the OTP is only ever emailed — never returned in the API
        # response. This field exists purely so the app is still demoable
        # without external accounts configured.
        "demo_otp": otp,
        "expires_in_seconds": OTP_TTL_SECONDS,
    }


@app.post("/api/patient/{pid}/verify-access")
async def verify_access(pid: str, req: AccessVerify):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    token = _verify_otp(pid, req.otp, req.provider_name)
    return {"ok": True, "access_token": token, "expires_in_seconds": ACCESS_TOKEN_TTL_SECONDS}


@app.get("/api/patient/{pid}/consent-log")
async def consent_log(pid: str):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    return {"ok": True, "log": list(reversed(CONSENT_LOG.get(pid, [])))[:30]}


@app.post("/api/patient/{pid}/visit")
async def log_visit(pid: str, v: VisitLog, x_access_token: Optional[str] = Header(None)):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found — register the patient first")
    _require_access(pid, x_access_token)

    record = v.model_dump()
    record["timestamp"] = time.time()
    record["date"] = datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
    VISITS.setdefault(pid, []).append(record)

    text = (
        f"Visit on {record['date']} — Provider: {v.provider_name} ({v.provider_type})"
        + (f" at {v.facility}." if v.facility else ".")
        + (f" Symptoms: {v.symptoms}." if v.symptoms else "")
        + (f" Diagnosis: {v.diagnosis}." if v.diagnosis else "")
        + (f" Prescription: {v.prescription}." if v.prescription else "")
        + (f" Allergy noted: {v.allergies_noted}." if v.allergies_noted else "")
        + (f" Notes: {v.notes}." if v.notes else "")
    )
    await mem_remember(text, patient_id=pid)
    return {"ok": True, "visit": record}


@app.get("/api/patient/{pid}/timeline")
async def timeline(pid: str, x_access_token: Optional[str] = Header(None)):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    _require_access(pid, x_access_token)
    return {"ok": True, "visits": VISITS.get(pid, [])}


# Keywords used to flag an obvious cross-visit allergy/prescription conflict
# in the graph view. This is a lightweight, deterministic app-side check —
# separate from (and in addition to) the free-text reasoning Claude does
# over Cognee's recalled memories in /ask.
_CONFLICT_PAIRS = [
    (("penicillin",), ("penicillin", "amoxicillin", "ampicillin")),
    (("ibuprofen", "nsaid"), ("ibuprofen", "nsaid", "diclofenac")),
    (("sulfa",), ("sulfa", "sulfamethoxazole", "co-trimoxazole")),
]


@app.get("/api/patient/{pid}/graph")
async def patient_graph(pid: str, x_access_token: Optional[str] = Header(None)):
    """Derives an entity/relationship graph from this patient's visits —
    patient, providers, facilities, diagnoses, prescriptions, and allergies
    as nodes, connected by the visits that mention them. Recurring entities
    (the same allergy or provider mentioned in more than one visit) collapse
    into a single shared node, which is what visually shows *why* cross-visit
    reasoning is possible: two otherwise-unconnected visits both touch the
    same allergy node, so a provider on visit 3 can be warned about something
    noted only on visit 1.

    This view is built directly from the same visit records that feed
    Cognee's remember()/recall() calls (see /ask for Cognee-grounded Q&A) —
    it renders the connections in that data as a graph rather than dumping
    Cognee's internal store, which keeps it fast, dependency-free, and
    testable independent of any live LLM key."""
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    _require_access(pid, x_access_token)

    visits = VISITS.get(pid, [])
    patient = PATIENTS[pid]
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, str]] = []

    def node(node_id: str, label: str, kind: str) -> str:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, "type": kind}
        return node_id

    patient_node = node("patient", patient.get("name", "Patient"), "patient")

    for i, v in enumerate(visits):
        visit_id = node(f"visit_{i}", v.get("date", f"Visit {i+1}"), "visit")
        edges.append({"from": patient_node, "to": visit_id, "label": "had visit"})

        if v.get("provider_name"):
            prov_id = node(f"prov_{re.sub(r'[^a-z0-9]+', '_', v['provider_name'].lower())}", v["provider_name"], "provider")
            edges.append({"from": visit_id, "to": prov_id, "label": "seen by"})

        if v.get("facility"):
            fac_id = node(f"fac_{re.sub(r'[^a-z0-9]+', '_', v['facility'].lower())}", v["facility"], "facility")
            edges.append({"from": visit_id, "to": fac_id, "label": "at"})

        if v.get("diagnosis"):
            diag_id = node(f"diag_{i}", v["diagnosis"], "diagnosis")
            edges.append({"from": visit_id, "to": diag_id, "label": "diagnosed"})

        rx_id = None
        if v.get("prescription"):
            rx_id = node(f"rx_{i}", v["prescription"], "prescription")
            edges.append({"from": visit_id, "to": rx_id, "label": "prescribed"})

        allergy_id = None
        if v.get("allergies_noted"):
            allergy_key = re.sub(r'[^a-z0-9]+', '_', v["allergies_noted"].lower())[:40]
            allergy_id = node(f"allergy_{allergy_key}", v["allergies_noted"], "allergy")
            edges.append({"from": visit_id, "to": allergy_id, "label": "allergy noted"})

    # Flag conflicts between any allergy node and any prescription node,
    # even across different visits / different providers.
    allergy_nodes = [n for n in nodes.values() if n["type"] == "allergy"]
    rx_nodes = [n for n in nodes.values() if n["type"] == "prescription"]
    for a in allergy_nodes:
        a_text = a["label"].lower()
        for rx in rx_nodes:
            rx_text = rx["label"].lower()
            for allergy_kw, rx_kws in _CONFLICT_PAIRS:
                if any(k in a_text for k in allergy_kw) and any(k in rx_text for k in rx_kws):
                    edges.append({"from": a["id"], "to": rx["id"], "label": "⚠ CONFLICT", "conflict": True})

    return {"ok": True, "nodes": list(nodes.values()), "edges": edges}


@app.post("/api/patient/{pid}/ask")
async def ask(pid: str, req: AskRequest, x_access_token: Optional[str] = Header(None)):
    """The core moment: any provider — a new doctor, an ASHA worker, a
    district hospital — asks a question and gets the FULL history back,
    synthesized by Claude but grounded strictly in what Cognee recalls.
    Requires a verified consent/access token for this patient."""
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    _require_access(pid, x_access_token)

    memories = await mem_recall(req.question, patient_id=pid)

    answer = None
    if LLM_API_KEY:
        try:
            answer = await _claude_clinical_answer(req.question, memories, PATIENTS[pid])
        except Exception:
            answer = None

    if answer is None:
        joined = "\n".join(f"- {m}" for m in memories[:6])
        answer = f"Recorded history relevant to this question:\n{joined}\n\n(Add LLM_API_KEY in .env for a synthesized clinical summary.)"

    return {"ok": True, "answer": answer, "raw_memories": memories, "asked_by": req.asked_by}


async def _claude_clinical_answer(question: str, memories: List[str], patient: Dict[str, Any]) -> str:
    import httpx

    context = "\n".join(f"- {m}" for m in memories) if memories else "(no prior records found)"
    system = (
        "You are Setu Swasth, a clinical memory assistant supporting healthcare "
        "providers (doctors, ASHA workers, PHC nurses, pharmacists) across India's "
        "fragmented rural healthcare system. You ONLY use the memory records provided "
        "below — never invent a symptom, diagnosis, allergy, or medicine that isn't in "
        "the records. If the records don't answer the question, say so plainly. Always "
        "flag allergies and drug conflicts prominently if relevant. Keep answers short, "
        "clear, and usable by a busy clinician or a community health worker with "
        "limited time."
    )
    user_content = (
        f"Patient: {patient.get('name')}, age {patient.get('age')}, {patient.get('gender')}, "
        f"village: {patient.get('village')}.\n\n"
        f"Recorded memory (from Cognee):\n{context}\n\n"
        f"Question: {question}"
    )
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 500,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {"content-type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts).strip() or "..."


@app.post("/api/patient/{pid}/improve")
async def improve_patient_memory(pid: str):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    result = await mem_improve(patient_id=pid)
    return {"ok": True, "result": result}


@app.post("/api/patient/{pid}/forget")
async def forget_patient(pid: str, req: ForgetRequest, x_access_token: Optional[str] = Header(None)):
    if pid not in PATIENTS:
        raise HTTPException(404, "patient not found")
    access = _require_access(pid, x_access_token)
    result = await mem_forget(patient_id=pid)
    VISITS[pid] = []
    _consent_log(pid, "memory_erased", access["provider_name"])
    return {"ok": True, "result": result, "reason": req.reason}


@app.get("/api/memory-log")
async def memory_log(limit: int = 40):
    """Live feed of Cognee operations — powers the 'Memory Lens' panel on
    the frontend so judges can see Cognee being called in real time."""
    return {"ok": True, "log": list(reversed(MEMORY_LOG))[:limit], "engine": "cognee" if COGNEE_AVAILABLE else "fallback-in-memory"}


# --------------------------------------------------------------------------
# Seed a demo patient so the product is instantly explorable
# --------------------------------------------------------------------------

@app.on_event("startup")
async def seed_demo_patient():
    if PATIENTS:
        return
    pid = "SS-DEMO001"
    PATIENTS[pid] = {
        "patient_id": pid, "name": "Kamala Devi", "age": 62, "gender": "Female",
        "village": "Bhavanipur, Bihar", "phone": "98XXXXXX10", "created": time.time(),
    }
    VISITS[pid] = []
    seed_visits = [
        dict(provider_name="Sunita (ASHA Worker)", provider_type="ASHA Worker", facility="Home visit",
             symptoms="Persistent cough, mild fever", diagnosis="Suspected seasonal flu",
             prescription="Paracetamol 500mg", allergies_noted=None,
             notes="Advised rest and fluids; referred to PHC if fever continues 3+ days."),
        dict(provider_name="Dr. R. Prasad", provider_type="Doctor", facility="PHC Bhavanipur",
             symptoms="Fever persisted, joint pain", diagnosis="Suspected dengue - advised test",
             prescription="Avoid ibuprofen; paracetamol only", allergies_noted="Penicillin allergy (rash on prior use)",
             notes="Platelet count monitored. No NSAIDs due to dengue suspicion."),
        dict(provider_name="Dr. Meera Iyer", provider_type="Doctor", facility="District Hospital, Patna",
             symptoms="Follow-up, fatigue", diagnosis="Dengue confirmed, recovering",
             prescription="Continued paracetamol, iron supplement", allergies_noted="Penicillin allergy (confirmed)",
             notes="Discharged with advice to follow up with PHC in 1 week."),
    ]
    for i, v in enumerate(seed_visits):
        record = dict(v)
        record["timestamp"] = time.time() - (len(seed_visits) - i) * 86400
        record["date"] = datetime.utcfromtimestamp(record["timestamp"]).strftime("%d %b %Y, %H:%M UTC")
        VISITS[pid].append(record)
        text = (
            f"Visit on {record['date']} — Provider: {v['provider_name']} ({v['provider_type']}) at {v['facility']}. "
            f"Symptoms: {v['symptoms']}. Diagnosis: {v['diagnosis']}. Prescription: {v['prescription']}. "
            + (f"Allergy noted: {v['allergies_noted']}. " if v.get('allergies_noted') else "")
            + f"Notes: {v['notes']}."
        )
        await mem_remember(text, patient_id=pid)

    # --- Second demo patient: deliberately built to make cross-visit
    # reasoning obvious. A PHC nurse notes a penicillin allergy in March;
    # five months later, with no record-sharing between facilities, a
    # different pharmacy prescribes an amoxicillin course for an unrelated
    # ear infection. Neither provider saw the other's note — only a system
    # that remembers across the whole chain can catch it.
    pid2 = "SS-DEMO002"
    PATIENTS[pid2] = {
        "patient_id": pid2, "name": "Ramesh Yadav", "age": 34, "gender": "Male",
        "village": "Chandpur, Uttar Pradesh", "phone": "98XXXXXX22", "created": time.time(),
    }
    VISITS[pid2] = []
    seed_visits_2 = [
        dict(provider_name="Nurse Anjali Verma", provider_type="Nurse", facility="PHC Chandpur",
             symptoms="Skin rash and swelling after a course of antibiotics", diagnosis="Drug allergy — confirmed penicillin sensitivity",
             prescription="Antihistamine (cetirizine); antibiotics discontinued", allergies_noted="Penicillin allergy (rash + swelling, confirmed by nurse observation)",
             notes="Patient advised to inform every future provider of this allergy.", days_ago=150),
        dict(provider_name="Dr. Feroz Khan", provider_type="Doctor", facility="Chandpur Family Clinic",
             symptoms="Recurring lower back pain, no fever", diagnosis="Mild lumbar strain",
             prescription="Paracetamol, physiotherapy referral", allergies_noted=None,
             notes="No red-flag symptoms; advised rest and light stretching.", days_ago=95),
        dict(provider_name="Pharmacist S. Gupta", provider_type="Pharmacist", facility="Chandpur Medical Store",
             symptoms="Ear pain and discharge, suspected infection", diagnosis="Suspected otitis media (ear infection)",
             prescription="Amoxicillin 500mg, 3x daily for 7 days", allergies_noted=None,
             notes="Sold over the counter; patient did not mention any prior allergy at time of purchase.", days_ago=5),
    ]
    for i, v in enumerate(seed_visits_2):
        record = {k: val for k, val in v.items() if k != "days_ago"}
        record["timestamp"] = time.time() - v["days_ago"] * 86400
        record["date"] = datetime.utcfromtimestamp(record["timestamp"]).strftime("%d %b %Y, %H:%M UTC")
        VISITS[pid2].append(record)
        text = (
            f"Visit on {record['date']} — Provider: {v['provider_name']} ({v['provider_type']}) at {v['facility']}. "
            f"Symptoms: {v['symptoms']}. Diagnosis: {v['diagnosis']}. Prescription: {v['prescription']}. "
            + (f"Allergy noted: {v['allergies_noted']}. " if v.get('allergies_noted') else "")
            + f"Notes: {v['notes']}."
        )
        await mem_remember(text, patient_id=pid2)


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_FRONTEND_DIR):
    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

    @app.get("/config.js")
    async def serve_config_js():
        # Backend-served mode: always same-origin, so force auto-detect
        # regardless of what's checked into frontend/config.js (that file's
        # contents only matter for a separately hosted Vercel frontend).
        return Response(content='window.ANAMNIS_API_BASE = "";\n', media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
