"""End-to-end checks of every flow. Run:  python tests/test_flows.py
Uses a throwaway data folder and simulated channels, so it never sends real messages."""
import io
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = tempfile.mkdtemp()
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "SMTP_HOST", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
          "OPENAI_API_KEY", "WHATSAPP_OVERRIDE_TO", "EMAIL_OVERRIDE_TO", "PUBLIC_BASE_URL"):
    os.environ[k] = ""
os.environ["INBOUND_API_KEY"] = "test-key"
sys.path.insert(0, str(ROOT))

import seed  # noqa: E402

seed.main()
from fastapi.testclient import TestClient  # noqa: E402

from app import db, inbound, main  # noqa: E402

ok = 0


def check(cond, msg):
    global ok
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    ok += 1
    print("  ok ", msg)


def login(c, u, pw):
    c.post("/login", data={"username": u, "password": pw})
    pid = f"CF-P{u[-3:]}" if u.startswith("p0") else "CF-P001"
    html = c.get(f"/patients/{pid}").text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def wait_query(qid, timeout=10):
    for _ in range(timeout * 10):
        r = db.q1("SELECT status FROM queries WHERE id=?", (qid,))
        if r and r["status"] not in ("received", "processing"):
            return r["status"]
        time.sleep(0.1)
    return None


print("Query letter → message")
with TestClient(main.app) as c:
    tok = login(c, "agent.priya", "Agent@2026")
    pdf = (ROOT / "sample_queries" / "CF-P001_star_query.pdf").read_bytes()
    t0 = time.time()
    r = c.post("/patients/CF-P001/queries", data={"csrf": tok}, files={"file": ("letter.pdf", pdf, "application/pdf")})
    check(r.status_code == 200, "agent uploads a query letter")
    qid = db.q1("SELECT max(id) m FROM queries")["m"]
    check(wait_query(qid) == "sent", f"query processed and patient notified in {time.time() - t0:.2f}s")
    pts = db.q("SELECT * FROM query_points WHERE query_id=?", (qid,))
    check(len(pts) == 5, "all 5 points identified")
    msgs = db.q("SELECT * FROM messages WHERE query_id=?", (qid,))
    check({m["channel"] for m in msgs} == {"whatsapp", "email"}, "message logged on WhatsApp and email")
    body = msgs[0]["body"]
    check("Cancelled cheque" not in body.split("We already have")[0], "does not ask for cheque already in locker")
    check("Consultation papers" in body and "Past medical" in body, "asks for the two missing items")

    print("Resend is de-duplicated")
    c.post(f"/queries/{qid}/resend", data={"csrf": tok})
    check(len(db.q("SELECT * FROM messages WHERE query_id=?", (qid,))) == 2, "identical message not sent twice")

    print("Agent override")
    p1 = db.q1("SELECT * FROM query_points WHERE query_id=? AND idx=1", (qid,))
    c.post(f"/queries/{qid}/points/{p1['id']}", data={"decision": "not_needed", "csrf": tok})
    check(db.q1("SELECT decision FROM query_points WHERE id=?", (p1["id"],))["decision"] == "not_needed", "decision overridden")

    print("Access control")
    check(c.get("/patients/CF-P006").status_code == 403, "agent blocked from another agent's patient")
    check(c.get("/files/doc/" + str(db.q1("SELECT id FROM documents WHERE patient_id='CF-P006'")["id"])).status_code == 403,
          "agent blocked from another agent's file")
    check(c.post("/patients/CF-P001/queries", files={"file": ("x.pdf", pdf)}).status_code == 400, "form without CSRF token rejected")
    check(c.get("/settings").status_code == 403, "agent can't open settings")

print("Patient replies on WhatsApp")
sent = []
inbound._send_wa = lambda to, body: sent.append(body) or ("simulated", "", "")


def make_pdf(title):
    from reportlab.pdfgen import canvas
    b = io.BytesIO()
    cv = canvas.Canvas(b)
    cv.drawString(72, 750, title)
    cv.drawString(72, 730, "Patient: Ravi Kumar Sharma")
    cv.save()
    return b.getvalue()


media = {"u1": make_pdf("Consultation paper - prescription from Dr. Rao"),
         "u2": make_pdf("Past medical records - previous treatment history")}
inbound.download_twilio_media = lambda url: media[url]
db.ex("UPDATE patients SET phone='+919999900001' WHERE id='CF-P001'")
with TestClient(main.app) as c:
    r = c.post("/webhooks/twilio/whatsapp", data={"From": "whatsapp:+919999900001", "Body": "", "NumMedia": "1",
                                                   "MediaUrl0": "u1", "MediaContentType0": "application/pdf", "MessageSid": "SM1"})
    check(r.status_code == 200, "webhook accepted")
    check(db.q1("SELECT * FROM documents WHERE source='whatsapp' AND doc_type='consultation_papers'") is not None,
          "WhatsApp file classified into the locker")
    check("Still pending" in sent[-1] and "Past medical" in sent[-1], "auto-reply lists what's still pending")
    check(db.q1("SELECT status FROM queries WHERE id=?", (qid,))["status"] == "partially_received", "query marked partly received")
    c.post("/webhooks/twilio/whatsapp", data={"From": "whatsapp:+919999900001", "NumMedia": "1",
                                               "MediaUrl0": "u1", "MediaContentType0": "application/pdf", "MessageSid": "SM2"})
    check("already had this one" in sent[-1], "duplicate file recognised")
    c.post("/webhooks/twilio/whatsapp", data={"From": "whatsapp:+919999900001", "NumMedia": "1",
                                               "MediaUrl0": "u2", "MediaContentType0": "application/pdf", "MessageSid": "SM3"})
    check(db.q1("SELECT status FROM queries WHERE id=?", (qid,))["status"] == "complete", "query complete once everything arrives")
    check("everything we need" in sent[-1], "patient told nothing else is pending")

print("Patient login")
with TestClient(main.app) as c:
    login(c, "p001", "Patient@2026")
    check(c.get("/patients/CF-P001").status_code == 200, "patient sees own locker")
    check(c.get("/patients/CF-P002").status_code == 403, "patient blocked from others")
    check(c.get("/").status_code == 200 and "Ravi" in c.get("/").text, "patient dashboard redirects to own page")

print("Bulk import + API + reminders")
with TestClient(main.app) as c:
    tok = login(c, "admin", "CareFi@2026")
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("patients.csv", "Patient ID,Name,Mobile,Email,Insurer,Claim Number,Agent\n"
                                    "CF-P011,Meera Joshi,+919811100011,meera@example.com,Star Health,CLM-11,agent.priya\n")
        zf.writestr("CF-P011/aadhaar.pdf", make_pdf("Aadhaar - Government of India - Meera Joshi"))
        zf.writestr("CF-P011/discharge.pdf", make_pdf("Discharge Summary - Meera Joshi"))
        zf.writestr("queries/CF-P011_query.pdf", (ROOT / "sample_queries" / "CF-P002_hdfc_query.pdf").read_bytes())
    r = c.post("/import", data={"csrf": tok}, files={"file": ("bundle.zip", z.getvalue(), "application/zip")})
    check(r.status_code == 200 and db.q1("SELECT * FROM patients WHERE id='CF-P011'"), "patient imported from ZIP")
    check(db.q1("SELECT count(*) n FROM documents WHERE patient_id='CF-P011'")["n"] == 2, "documents imported and classified")
    q11 = db.q1("SELECT * FROM queries WHERE patient_id='CF-P011'")
    check(q11 and q11["status"].startswith("sent"), "imported query letter processed")
    body = db.q1("SELECT body FROM messages WHERE query_id=?", (q11["id"],))["body"]
    check("Aadhaar" not in body.split("We already have")[0], "Aadhaar from import not asked again")
    r = c.post("/api/patients/CF-P003/queries", headers={"x-api-key": "test-key"},
               files={"file": ("q.pdf", (ROOT / "sample_queries" / "CF-P003_icici_query.pdf").read_bytes())})
    check(r.status_code == 200 and wait_query(r.json()["query_id"]), "API upload processed")
    check(c.post("/api/patients/CF-P003/queries", headers={"x-api-key": "nope"}, files={"file": ("q.pdf", b"x")}).status_code == 401,
          "API rejects a bad key")
    db.ex("UPDATE queries SET deadline_at='2000-01-01' WHERE id=?", (q11["id"],))
    from app.pipeline import run_due_reminders
    run_due_reminders()
    check(db.q1("SELECT * FROM messages WHERE query_id=? AND kind='reminder'", (q11["id"],)), "reminder sent before deadline")
    for path in ("/", "/?view=attention", "/settings", "/import", "/audit", "/patients/CF-P011", "/patients/new"):
        check(c.get(path).status_code == 200, f"admin page {path} renders")

print("Meta WhatsApp Cloud API")
import hashlib, hmac, json as _json
os.environ.update({"META_VERIFY_TOKEN": "vt-123", "META_APP_SECRET": "sec"})
media["meta:MID1"] = make_pdf("Aadhaar card - Government of India - Ravi Kumar Sharma")
sent.clear()
with TestClient(main.app) as c:
    r = c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "vt-123", "hub.challenge": "42"})
    check(r.status_code == 200 and r.text == "42", "Meta webhook verification handshake")
    check(c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "1"}).status_code == 403,
          "wrong verify token rejected")
    payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "919999900001", "id": "wamid.X1", "type": "document",
               "document": {"id": "MID1", "mime_type": "application/pdf", "filename": "a.pdf"}}]}}]}]}
    raw = _json.dumps(payload).encode()
    check(c.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403,
          "Meta webhook with bad signature rejected")
    sig = "sha256=" + hmac.new(b"sec", raw, hashlib.sha256).hexdigest()
    check(c.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": sig}).status_code == 200, "signed Meta webhook accepted")
    check(db.q1("SELECT * FROM documents WHERE patient_id='CF-P001' AND doc_type='kyc_aadhaar' AND source='whatsapp'") is not None,
          "Meta document downloaded and classified into the locker")
    check(sent and "received" in sent[-1], "auto-reply sent after Meta message")
    db.ex("UPDATE messages SET provider_ref='wamid.OUT1' WHERE id=(SELECT max(id) FROM messages WHERE direction='out')")
    st = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.OUT1", "status": "read"}]}}]}]}
    raw = _json.dumps(st).encode()
    c.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": "sha256=" + hmac.new(b"sec", raw, hashlib.sha256).hexdigest()})
    check(db.q1("SELECT status FROM messages WHERE provider_ref='wamid.OUT1'")["status"] == "read", "read receipt recorded")

print(f"\nAll {ok} checks passed.")
