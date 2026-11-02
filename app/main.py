import json
import os
import secrets
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from . import db  # noqa: E402
from .importer import import_bundle  # noqa: E402
from .inbound import handle_meta, handle_whatsapp  # noqa: E402
from .llm import engine_name  # noqa: E402
from .notify import email_configured, email_provider, meta_signature_ok, send_email, send_whatsapp, twilio_signature_ok, whatsapp_provider  # noqa: E402
from .pipeline import (OPEN_STATUSES, add_document, notify_for_query, pending_for_query, point_rows,  # noqa: E402
                       process_query, refresh_progress, run_due_reminders, send_reminder)
from .taxonomy import DOC_TYPES, PATIENT_SOURCED, label  # noqa: E402

db.init()
app = FastAPI(title="care.fi Patient Locker")
app.add_middleware(SessionMiddleware, secret_key=(os.getenv("SECRET_KEY") if os.getenv("SECRET_KEY") not in (None, "", "change-me-to-a-long-random-string") else secrets.token_hex(32)),
                   https_only=os.getenv("HTTPS_ONLY") == "1", same_site="lax", max_age=8 * 3600)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
MAX_BYTES = 15 * 1024 * 1024
STATUS_LABEL = {
    "received": "Received", "processing": "Reading letter", "sent": "Waiting on patient",
    "sent_needs_review": "Waiting on patient · review 1 point", "partially_received": "Partly received",
    "complete": "All items received", "no_patient_action": "Nothing needed from patient",
    "needs_review": "Needs agent review", "send_failed": "Send failed", "failed": "Processing failed",
}
DECISIONS = {"need_from_patient": "Ask patient", "in_locker": "Already in locker",
             "hospital_team": "Hospital team", "not_needed": "Not needed", "needs_review": "Agent review"}
ATTENTION = ("needs_review", "sent_needs_review", "send_failed", "failed")


def mask_phone(p):
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    return f"+{d[:2]} {d[2:4]}xxxxx{d[-3:]}" if len(d) >= 10 else "—"


def mask_email(e):
    if not e or "@" not in e:
        return "—"
    n, dom = e.split("@", 1)
    return f"{n[:2]}{'•' * max(1, len(n) - 2)}@{dom}"


def ist(ts):
    """UTC ISO -> '01 Nov, 11:42 PM' in IST."""
    if not ts:
        return ""
    from datetime import timedelta, timezone
    try:
        t = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=5, minutes=30)))
        return t.strftime("%d %b, %I:%M %p")
    except Exception:
        return ts[:16]


templates.env.globals.update(BRAND=os.getenv("BRAND_NAME", "care.fi"), label=label, DOC_TYPES=DOC_TYPES, jload=db.jload, STATUS_LABEL=STATUS_LABEL,
                             DECISIONS=DECISIONS, mask_phone=mask_phone, mask_email=mask_email)
templates.env.filters["ist"] = ist


# ---- auth & access -------------------------------------------------------------
def current_user(request: Request):
    uid = request.session.get("uid")
    return db.q1("SELECT * FROM users WHERE id=?", (uid,)) if uid else None


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


def can_see(user, patient_id: str) -> bool:
    if user["role"] == "admin":
        return True
    if user["role"] == "patient":
        return user["patient_id"] == patient_id
    p = db.q1("SELECT assigned_agent FROM patients WHERE id=?", (patient_id,))
    return bool(p) and p["assigned_agent"] == user["username"]


def guard(user, patient_id: str):
    if not can_see(user, patient_id):
        db.audit(user["username"], "DENIED", patient_id)
        raise HTTPException(403, "You don't have access to this patient")


def staff_only(user):
    if user["role"] not in ("admin", "agent"):
        raise HTTPException(403, "Staff only")


def admin_only(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")


def check_csrf(request: Request, token: str):
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(400, "Form expired, please reload the page and try again")


def page(request, name, ctx, status_code=200):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    ctx.setdefault("user", current_user(request))
    ctx["csrf"] = request.session["csrf"]
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request, msg):
    request.session["flash"] = msg


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"File type {ext or '?'} not allowed. Use PDF, JPG, PNG or TXT.")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "File larger than 15 MB")
    return file.filename or f"file{ext}", data


def store_query_file(pid: str, name: str, data: bytes, by: str) -> int:
    folder = db.UPLOAD_DIR / pid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"   # random name: nothing guessable on disk
    path.write_bytes(data)
    qid = db.ex("INSERT INTO queries(patient_id, filename, stored_path, uploaded_at, uploaded_by, status) VALUES (?,?,?,?,?, 'received')",
                (pid, name, str(path), db.now(), by))
    db.audit(by, "UPLOAD_QUERY", f"{pid}:q{qid}")
    return qid


@app.exception_handler(HTTPException)
async def http_err(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith(("/api/", "/webhooks/")):
        return JSONResponse({"error": exc.detail}, exc.status_code)
    return page(request, "error.html", {"code": exc.status_code, "msg": exc.detail}, exc.status_code)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---- login ---------------------------------------------------------------------
_fails: dict[str, list[float]] = {}


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return page(request, "guide.html", {"engine": engine_name(), "wa": whatsapp_provider(), "email_prov": email_provider()})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "login.html", {"user": None, "err": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    uname = username.strip().lower()
    recent = [t for t in _fails.get(uname, []) if time.time() - t < 600]
    if len(recent) >= 5:
        return page(request, "login.html", {"user": None, "err": "Too many attempts. Try again in 10 minutes."}, 429)
    u = db.q1("SELECT * FROM users WHERE username=?", (uname,))
    if not u or not db.check_pw(password, u["password_hash"]):
        _fails[uname] = recent + [time.time()]
        db.audit(uname, "LOGIN_FAILED")
        return page(request, "login.html", {"user": None, "err": "Wrong username or password"}, 401)
    _fails.pop(uname, None)
    request.session.clear()
    request.session["uid"] = u["id"]
    request.session["csrf"] = secrets.token_urlsafe(24)
    db.audit(u["username"], "LOGIN")
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# ---- dashboard ---------------------------------------------------------------------
def visible_patients(user):
    if user["role"] == "admin":
        return db.q("SELECT * FROM patients ORDER BY id")
    if user["role"] == "agent":
        return db.q("SELECT * FROM patients WHERE assigned_agent=? ORDER BY id", (user["username"],))
    return db.q("SELECT * FROM patients WHERE id=?", (user["patient_id"],))


def latency(qrow):
    if qrow.get("uploaded_at") and qrow.get("sent_at"):
        s = (datetime.fromisoformat(qrow["sent_at"]) - datetime.fromisoformat(qrow["uploaded_at"])).total_seconds()
        return f"{s:.1f}s" if s >= 0 else None
    return None


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, view: str = "all", q: str = ""):
    user = require_user(request)
    if user["role"] == "patient":
        return RedirectResponse(f"/patients/{user['patient_id']}", 303)
    pats = visible_patients(user)
    ids = [p["id"] for p in pats] or [""]
    ph = ",".join("?" * len(ids))
    docs = {r["patient_id"]: r["n"] for r in db.q(f"SELECT patient_id, COUNT(*) n FROM documents WHERE patient_id IN ({ph}) GROUP BY patient_id", ids)}
    flagged = {r["patient_id"]: r["n"] for r in db.q(f"SELECT patient_id, COUNT(*) n FROM documents WHERE flag IS NOT NULL AND patient_id IN ({ph}) GROUP BY patient_id", ids)}
    queries = db.q(f"SELECT * FROM queries WHERE patient_id IN ({ph}) ORDER BY id", ids)
    latest, pending = {}, {}
    for r in queries:
        latest[r["patient_id"]] = r
        if r["status"] in OPEN_STATUSES:
            pending[r["patient_id"]] = pending.get(r["patient_id"], 0) + len(pending_for_query(r)[1])
    counts = {
        "waiting": sum(1 for r in queries if r["status"] in OPEN_STATUSES),
        "attention": sum(1 for r in queries if r["status"] in ATTENTION) + sum(flagged.values()),
        "complete": sum(1 for r in queries if r["status"] in ("complete", "no_patient_action")),
        "patients": len(pats),
    }
    lat = [float(latency(r)[:-1]) for r in queries if latency(r)]
    counts["avg_latency"] = f"{sum(lat) / len(lat):.1f}s" if lat else "—"
    rows = []
    for p in pats:
        lq = latest.get(p["id"])
        needs = (lq and lq["status"] in ATTENTION) or flagged.get(p["id"])
        if view == "attention" and not needs:
            continue
        if view == "waiting" and not (lq and lq["status"] in OPEN_STATUSES):
            continue
        hay = " ".join(str(p.get(k) or "") for k in ("id", "name", "insurer", "claim_no", "hospital")).lower()
        if q and q.lower() not in hay:
            continue
        rows.append({**p, "lq": lq, "docs": docs.get(p["id"], 0), "flagged": flagged.get(p["id"], 0),
                     "pending": pending.get(p["id"], 0), "needs": needs})
    recent = db.q(f"""SELECT q.*, p.name FROM queries q JOIN patients p ON p.id=q.patient_id
                      WHERE q.patient_id IN ({ph}) ORDER BY q.id DESC LIMIT 8""", ids)
    for r in recent:
        r["latency"] = latency(r)
    return page(request, "dashboard.html", {
        "rows": rows, "recent": recent, "counts": counts, "view": view, "q": q,
        "engine": engine_name(), "wa": whatsapp_provider(), "email_on": email_configured()})


# ---- patients -------------------------------------------------------------------
PATIENT_FIELDS = ["name", "phone", "email", "insurer", "policy_no", "claim_no", "hospital", "admission_date",
                  "diagnosis", "assigned_agent"]


def agents():
    return db.q("SELECT username, display_name FROM users WHERE role='agent' ORDER BY username")


@app.get("/patients/new", response_class=HTMLResponse)
def new_patient(request: Request):
    user = require_user(request)
    admin_only(user)
    return page(request, "patient_form.html", {"p": {}, "agents": agents(), "new": True})


@app.post("/patients/new")
async def create_patient(request: Request):
    user = require_user(request)
    admin_only(user)
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    pid = (form.get("id") or "").strip().upper()
    if not pid or db.q1("SELECT 1 FROM patients WHERE id=?", (pid,)):
        flash(request, "That patient ID is empty or already in use.")
        return RedirectResponse("/patients/new", 303)
    from .importer import upsert_patient
    upsert_patient({"id": pid, **{k: (form.get(k) or "").strip() for k in PATIENT_FIELDS}}, user["username"])
    flash(request, f"Patient {pid} created. Login: {pid.lower().replace('-', '')} / default patient password.")
    return RedirectResponse(f"/patients/{pid}", 303)


@app.get("/patients/{pid}/edit", response_class=HTMLResponse)
def edit_patient(request: Request, pid: str):
    user = require_user(request)
    staff_only(user)
    guard(user, pid)
    return page(request, "patient_form.html", {"p": db.q1("SELECT * FROM patients WHERE id=?", (pid,)),
                                               "agents": agents(), "new": False})


@app.post("/patients/{pid}/edit")
async def save_patient(request: Request, pid: str):
    user = require_user(request)
    staff_only(user)
    guard(user, pid)
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    fields = [f for f in PATIENT_FIELDS if f != "assigned_agent" or user["role"] == "admin"]
    db.ex(f"UPDATE patients SET {', '.join(f + '=?' for f in fields)} WHERE id=?",
          (*[(form.get(f) or "").strip() for f in fields], pid))
    db.audit(user["username"], "EDIT_PATIENT", pid)
    flash(request, "Patient details saved.")
    return RedirectResponse(f"/patients/{pid}", 303)


@app.get("/patients/{pid}", response_class=HTMLResponse)
def patient_page(request: Request, pid: str):
    user = require_user(request)
    guard(user, pid)
    p = db.q1("SELECT * FROM patients WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Patient not found")
    db.audit(user["username"], "VIEW_PATIENT", pid)
    docs = db.q("SELECT * FROM documents WHERE patient_id=? ORDER BY uploaded_at DESC", (pid,))
    have = {d["doc_type"] for d in docs}
    queries = db.q("SELECT * FROM queries WHERE patient_id=? ORDER BY id DESC", (pid,))
    open_items = []
    for qr in queries:
        qr["points"] = point_rows(qr["id"])
        qr["latency"] = latency(qr)
        received, pend = pending_for_query(qr)
        qr["received"], qr["pending"] = received, pend
        if qr["status"] in OPEN_STATUSES:
            open_items += [c for c in pend if c not in open_items]
    msgs = db.q("SELECT * FROM messages WHERE patient_id=? ORDER BY id DESC", (pid,))
    checklist = [(c, DOC_TYPES[c]["label"], c in have) for c in
                 ("kyc_aadhaar", "kyc_pan", "policy_copy", "health_card", "discharge_summary", "final_bill",
                  "investigation_reports", "consultation_papers", "pharmacy_bills", "cancelled_cheque")]
    return page(request, "patient.html", {
        "p": p, "docs": docs, "queries": queries, "msgs": msgs, "checklist": checklist, "open_items": open_items,
        "doc_types": sorted(DOC_TYPES.items(), key=lambda kv: kv[1]["label"])})


# ---- documents ---------------------------------------------------------------------
@app.post("/patients/{pid}/documents")
async def upload_documents(request: Request, pid: str, files: list[UploadFile] = File(...),
                           doc_type: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    guard(user, pid)
    check_csrf(request, csrf)
    results = []
    for f in files:
        name, data = await read_upload(f)
        d = add_document(pid, name, data, by=user["username"], source="portal",
                         doc_type=doc_type if (doc_type in DOC_TYPES and len(files) == 1) else None)
        results.append(f"{name} → {label(d['doc_type'])}" + (" (duplicate, skipped)" if d.get("duplicate") else ""))
    flash(request, "Added: " + "; ".join(results))
    return RedirectResponse(f"/patients/{pid}#documents", 303)


@app.post("/documents/{did}/type")
def set_doc_type(request: Request, did: int, doc_type: str = Form(...), csrf: str = Form("")):
    user = require_user(request)
    staff_only(user)
    check_csrf(request, csrf)
    d = db.q1("SELECT * FROM documents WHERE id=?", (did,))
    if not d or doc_type not in DOC_TYPES:
        raise HTTPException(404)
    guard(user, d["patient_id"])
    db.ex("UPDATE documents SET doc_type=?, classified_by='agent', confidence=1, flag=NULL WHERE id=?", (doc_type, did))
    db.audit(user["username"], "RETYPE_DOC", f"{d['patient_id']}:{did}:{doc_type}")
    refresh_progress(d["patient_id"])
    return RedirectResponse(f"/patients/{d['patient_id']}#documents", 303)


@app.post("/documents/{did}/clear-flag")
def clear_flag(request: Request, did: int, csrf: str = Form("")):
    user = require_user(request)
    staff_only(user)
    check_csrf(request, csrf)
    d = db.q1("SELECT * FROM documents WHERE id=?", (did,))
    if not d:
        raise HTTPException(404)
    guard(user, d["patient_id"])
    db.ex("UPDATE documents SET flag=NULL WHERE id=?", (did,))
    db.audit(user["username"], "CLEAR_FLAG", f"{d['patient_id']}:{did}")
    return RedirectResponse(f"/patients/{d['patient_id']}#documents", 303)


# ---- queries ---------------------------------------------------------------------
@app.post("/patients/{pid}/queries")
async def upload_query(request: Request, pid: str, bg: BackgroundTasks, file: UploadFile = File(...), csrf: str = Form("")):
    user = require_user(request)
    staff_only(user)
    guard(user, pid)
    check_csrf(request, csrf)
    name, data = await read_upload(file)
    qid = store_query_file(pid, name, data, user["username"])
    bg.add_task(process_query, qid)          # starts immediately, no button to press
    return RedirectResponse(f"/patients/{pid}#q{qid}", 303)


def _query_for(request, qid):
    user = require_user(request)
    staff_only(user)
    qr = db.q1("SELECT * FROM queries WHERE id=?", (qid,))
    if not qr:
        raise HTTPException(404, "Query not found")
    guard(user, qr["patient_id"])
    return user, qr


@app.post("/queries/{qid}/reprocess")
def reprocess(request: Request, qid: int, bg: BackgroundTasks, csrf: str = Form("")):
    user, qr = _query_for(request, qid)
    check_csrf(request, csrf)
    db.ex("UPDATE queries SET uploaded_at=?, sent_at=NULL, error=NULL, completed_at=NULL WHERE id=?", (db.now(), qid))
    db.audit(user["username"], "REPROCESS", f"{qr['patient_id']}:q{qid}")
    bg.add_task(process_query, qid)
    return RedirectResponse(f"/patients/{qr['patient_id']}#q{qid}", 303)


@app.post("/queries/{qid}/points/{point_id}")
def override_point(request: Request, qid: int, point_id: int, decision: str = Form(...), csrf: str = Form("")):
    user, qr = _query_for(request, qid)
    check_csrf(request, csrf)
    pt = db.q1("SELECT * FROM query_points WHERE id=? AND query_id=?", (point_id, qid))
    if not pt or decision not in DECISIONS:
        raise HTTPException(404)
    codes = db.jload(pt["doc_types"])
    missing = [c for c in codes if c in PATIENT_SOURCED] if decision == "need_from_patient" else []
    if decision == "need_from_patient" and not missing:
        missing = ["patient_declaration"]
    db.ex("UPDATE query_points SET decision=?, missing_codes=?, overridden_by=? WHERE id=?",
          (decision, json.dumps(missing), user["username"], point_id))
    db.audit(user["username"], "OVERRIDE", f"{qr['patient_id']}:q{qid}:p{pt['idx']}={decision}")
    flash(request, f"Point {pt['idx']} set to '{DECISIONS[decision]}'. Use 'Send updated message' to notify the patient.")
    return RedirectResponse(f"/patients/{qr['patient_id']}#q{qid}", 303)


@app.post("/queries/{qid}/resend")
def resend(request: Request, qid: int, csrf: str = Form("")):
    user, qr = _query_for(request, qid)
    check_csrf(request, csrf)
    notify_for_query(qid)
    db.audit(user["username"], "RESEND", f"{qr['patient_id']}:q{qid}")
    flash(request, "Message rebuilt from the current decisions and sent (identical messages are not sent twice).")
    return RedirectResponse(f"/patients/{qr['patient_id']}#q{qid}", 303)


@app.post("/queries/{qid}/remind")
def remind(request: Request, qid: int, csrf: str = Form("")):
    user, qr = _query_for(request, qid)
    check_csrf(request, csrf)
    ok = send_reminder(qid, by=user["username"])
    flash(request, "Reminder sent for the items still pending." if ok else "Nothing pending, so no reminder was sent.")
    return RedirectResponse(f"/patients/{qr['patient_id']}#q{qid}", 303)


# ---- files ---------------------------------------------------------------------
@app.get("/files/{kind}/{fid}")
def download(request: Request, kind: str, fid: int):
    user = require_user(request)
    table = {"doc": "documents", "query": "queries"}.get(kind)
    if not table:
        raise HTTPException(404)
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", (fid,))
    if not row:
        raise HTTPException(404)
    guard(user, row["patient_id"])
    db.audit(user["username"], f"OPEN_{kind.upper()}", f"{row['patient_id']}:{fid}")
    return FileResponse(row["stored_path"], filename=row["filename"],
                        content_disposition_type="inline", headers={"Cache-Control": "no-store"})


# ---- admin -----------------------------------------------------------------------
@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    user = require_user(request)
    admin_only(user)
    return page(request, "audit.html", {"rows": db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")})


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    user = require_user(request)
    admin_only(user)
    return page(request, "import.html", {"report": None})


@app.post("/import", response_class=HTMLResponse)
async def do_import(request: Request, file: UploadFile = File(...), csrf: str = Form("")):
    user = require_user(request)
    admin_only(user)
    check_csrf(request, csrf)
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(400, "Import larger than 200 MB")
    report = import_bundle(file.filename or "upload.zip", data, user["username"], process_query, add_document)
    return page(request, "import.html", {"report": report})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_user(request)
    admin_only(user)
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return page(request, "settings.html", {
        "engine": engine_name(), "wa": whatsapp_provider(), "email_on": email_configured(), "email_prov": email_provider(),
        "wa_override": os.getenv("WHATSAPP_OVERRIDE_TO"), "email_override": os.getenv("EMAIL_OVERRIDE_TO"),
        "base": base, "webhook": f"{base or 'https://<your-public-url>'}/webhooks/twilio/whatsapp",
        "meta_webhook": f"{base or 'https://<your-public-url>'}/webhooks/meta",
        "meta_verify_set": bool(os.getenv("META_VERIFY_TOKEN")), "meta_secret_set": bool(os.getenv("META_APP_SECRET")),
        "users": db.q("SELECT username, role, display_name, patient_id FROM users ORDER BY role, username")})


@app.post("/settings/test")
def settings_test(request: Request, phone: str = Form(""), email: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    admin_only(user)
    check_csrf(request, csrf)
    out = []
    if phone:
        st, _, err, to = send_whatsapp(phone, "Test message from care.fi Patient Locker. WhatsApp is working.")
        out.append(f"WhatsApp to {to}: {st} {err}")
    if email:
        st, _, err, to = send_email(email, "care.fi Patient Locker test", "Email is working.")
        out.append(f"Email to {to}: {st} {err}")
    flash(request, " | ".join(out) or "Enter a phone or email.")
    return RedirectResponse("/settings", 303)


# ---- webhooks (Twilio) ------------------------------------------------------------
def _verified_form(request: Request, form) -> dict:
    params = {k: v for k, v in form.items()}
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    url = (base + request.url.path) if base else str(request.url)
    if not twilio_signature_ok(url, params, request.headers.get("x-twilio-signature", "")):
        db.audit("twilio", "WEBHOOK_BAD_SIGNATURE", request.url.path)
        raise HTTPException(403, "bad signature")
    return params


@app.post("/webhooks/twilio/whatsapp")
async def twilio_inbound(request: Request, bg: BackgroundTasks):
    params = _verified_form(request, await request.form())
    bg.add_task(handle_whatsapp, params)        # reply quickly; work happens right after
    return Response("<Response></Response>", media_type="application/xml")


@app.post("/webhooks/twilio/status")
async def twilio_status(request: Request):
    params = _verified_form(request, await request.form())
    st = params.get("MessageStatus", "")
    if st in ("delivered", "read", "failed", "undelivered"):
        db.ex("UPDATE messages SET status=?, error=COALESCE(?, error) WHERE provider_ref=?",
              ("failed" if st == "undelivered" else st, params.get("ErrorCode"), params.get("MessageSid")))
    return Response(status_code=204)


@app.get("/webhooks/meta")
def meta_verify(request: Request):
    p = request.query_params
    tok = os.getenv("META_VERIFY_TOKEN")
    if p.get("hub.mode") == "subscribe" and tok and secrets.compare_digest(p.get("hub.verify_token", ""), tok):
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "verification failed")


@app.post("/webhooks/meta")
async def meta_inbound(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not meta_signature_ok(raw, request.headers.get("x-hub-signature-256", "")):
        db.audit("meta", "WEBHOOK_BAD_SIGNATURE", "/webhooks/meta")
        raise HTTPException(403, "bad signature")
    bg.add_task(handle_meta, json.loads(raw or b"{}"))
    return Response(status_code=200)


# ---- JSON API -------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "engine": engine_name(), "whatsapp": whatsapp_provider(), "email": email_provider()}


@app.get("/api/queries/{qid}")
def api_query(request: Request, qid: int):
    user = require_user(request)
    qr = db.q1("SELECT id, patient_id, status, uploaded_at, processed_at, sent_at FROM queries WHERE id=?", (qid,))
    if not qr:
        raise HTTPException(404, "not found")
    guard(user, qr["patient_id"])
    return qr


@app.post("/api/patients/{pid}/queries")
async def api_upload_query(request: Request, pid: str, bg: BackgroundTasks, file: UploadFile = File(...)):
    key = os.getenv("INBOUND_API_KEY")
    if not key or key == "change-me" or not secrets.compare_digest(request.headers.get("x-api-key", ""), key):
        raise HTTPException(401, "bad api key")
    if not db.q1("SELECT id FROM patients WHERE id=?", (pid,)):
        raise HTTPException(404, "unknown patient")
    name, data = await read_upload(file)
    qid = store_query_file(pid, name, data, "api")
    bg.add_task(process_query, qid)
    return {"query_id": qid, "status": "received"}


# ---- background: drop-folder watcher + reminder scheduler ------------------------------
INBOX = db.BASE_DIR / "inbox"


def _watch_inbox():
    INBOX.mkdir(exist_ok=True)
    while True:
        try:
            for pdir in INBOX.iterdir():
                if not pdir.is_dir() or not db.q1("SELECT id FROM patients WHERE id=?", (pdir.name,)):
                    continue
                for f in pdir.iterdir():
                    if f.suffix.lower() not in ALLOWED or f.name.startswith(".") or time.time() - f.stat().st_mtime < 1:
                        continue
                    data = f.read_bytes()
                    f.unlink()
                    qid = store_query_file(pdir.name, f.name, data, "inbox")
                    threading.Thread(target=process_query, args=(qid,), daemon=True).start()
        except Exception as e:  # keep watching no matter what
            print("inbox watcher:", e)
        time.sleep(1)


def _reminders():
    while True:
        try:
            run_due_reminders()
        except Exception as e:
            print("reminders:", e)
        time.sleep(300)


def _resume_unfinished():
    """A restart (e.g. a redeploy) can interrupt a letter mid-read: pick those up again."""
    for qrow in db.q("SELECT id FROM queries WHERE status IN ('received','processing')"):
        threading.Thread(target=process_query, args=(qrow["id"],), daemon=True).start()


@app.on_event("startup")
def _start_background():
    _resume_unfinished()
    threading.Thread(target=_watch_inbox, daemon=True).start()
    threading.Thread(target=_reminders, daemon=True).start()
