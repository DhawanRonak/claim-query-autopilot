"""Query letter -> points -> locker check -> patient message -> WhatsApp + email.

Runs automatically the moment a query letter lands in a patient's locker, and
keeps each query's progress up to date as the patient sends documents back.
"""
import json
import os
import re
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from pypdf import PdfReader

from . import db
from .classify import classify
from .llm import WHY, extract_points
from .notify import send_email, send_whatsapp
from .taxonomy import DOC_TYPES, PATIENT_SOURCED, label

TEMPLATE_PATH = db.BASE_DIR / "templates_msg" / "patient_query.txt"
REMINDER_PATH = db.BASE_DIR / "templates_msg" / "patient_reminder.txt"
OPEN_STATUSES = ("sent", "sent_needs_review", "partially_received")


def read_text(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        try:
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
        except Exception:
            return ""
    if p.suffix.lower() in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="ignore")
    return ""  # images: the AI reads the file itself


# ---- documents ----------------------------------------------------------------
def add_document(patient_id: str, filename: str, data: bytes, by: str, source: str = "portal",
                 doc_type: str | None = None, notes: str | None = None) -> dict:
    """Save a file into the locker, classify it from its contents, update query progress."""
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    dup = db.q1("SELECT * FROM documents WHERE patient_id=? AND sha256=?", (patient_id, digest))
    if dup:
        return {**dup, "duplicate": True}
    ext = Path(filename).suffix.lower() or ".bin"
    folder = db.UPLOAD_DIR / patient_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    patient = db.q1("SELECT * FROM patients WHERE id=?", (patient_id,))
    if doc_type in DOC_TYPES:
        res = {"doc_type": doc_type, "confidence": 1.0, "classified_by": "agent", "flag": None}
    else:
        res = classify(read_text(str(path)), data, filename, patient["name"])
    did = db.ex("""INSERT INTO documents(patient_id, doc_type, filename, stored_path, uploaded_at, uploaded_by,
                   notes, confidence, classified_by, flag, source, sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (patient_id, res["doc_type"], filename, str(path), db.now(), by, notes,
                 res["confidence"], res["classified_by"], res["flag"], source, digest))
    db.audit(by, "UPLOAD_DOC", f"{patient_id}:{res['doc_type']}")
    refresh_progress(patient_id)
    return {"id": did, "duplicate": False, **res}


# ---- step 2: decide ------------------------------------------------------------
def decide(points: list[dict], locker: list[dict]) -> list[dict]:
    have = {}
    for d in locker:
        have.setdefault(d["doc_type"], []).append(d)

    decided = []
    for p in points:
        codes = p["doc_types"]
        present = [c for c in codes if c in have]
        missing = [c for c in codes if c not in have]
        # insurer rejected our earlier copy -> ask again even though it's in the locker
        if p.get("fresh_copy_needed"):
            redo = [c for c in present if c in PATIENT_SOURCED]
            present = [c for c in present if c not in redo]
            missing += redo
        miss_patient = [c for c in missing if c in PATIENT_SOURCED]
        miss_hospital = [c for c in missing if c not in PATIENT_SOURCED]

        if not codes:
            decision = "needs_review"
        elif miss_patient:
            decision = "need_from_patient"
        elif miss_hospital:
            decision = "hospital_team"
        else:
            decision = "in_locker"

        decided.append({**p, "decision": decision, "present": present, "missing_patient": miss_patient,
                        "missing_hospital": miss_hospital,
                        "matched_doc_ids": [d["id"] for c in present for d in have[c]]})
    return decided


def point_rows(query_id: int) -> list[dict]:
    rows = db.q("SELECT * FROM query_points WHERE query_id=? ORDER BY idx", (query_id,))
    for r in rows:
        r["doc_types"] = db.jload(r["doc_types"])
        r["missing_patient"] = db.jload(r["missing_codes"])
        r["present"] = db.jload(r["present_codes"])
    return rows


# ---- step 3: compose -----------------------------------------------------------
def load_template(path: Path) -> tuple[str, str]:
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
    subject = "Action needed for your insurance claim"
    if lines and lines[0].startswith("SUBJECT:"):
        subject = lines.pop(0)[len("SUBJECT:"):].strip()
    return subject, "\n".join(lines).strip()


def deadline_date(raw: str) -> datetime:
    """'7 days' -> a real date, keeping a 1-day buffer so we can forward to the insurer in time."""
    m = re.search(r"(\d+)\s*(working\s+)?days", raw or "", re.I)
    days = max(1, int(m.group(1)) - 1) if m else int(os.getenv("DEFAULT_DEADLINE_DAYS", "3"))
    return datetime.now() + timedelta(days=days)


def _declaration_line(text: str) -> str:
    detail = re.sub(r"^(please|kindly)?\s*(also)?\s*(clarify|explain|confirm|state|mention|provide|submit)\s*(on|about|regarding)?\s*",
                    "", text.strip(), flags=re.I).rstrip(".")
    detail = re.sub(r"\bthe insured's\b", "your", detail, flags=re.I)
    detail = re.sub(r"\bthe insured (was|is|has)\b",
                    lambda m: "you " + {"was": "were", "is": "are", "has": "have"}[m.group(1).lower()], detail, flags=re.I)
    detail = re.sub(r"\bthe (insured|patient)\b", "you", detail, flags=re.I)
    return f"A short signed note from you explaining {detail}"


def ask_lines(points: list[dict], only_codes: set | None = None) -> list[str]:
    """One line per document, even if two points asked for the same thing."""
    asks, seen = [], set()
    for p in points:
        if p["decision"] != "need_from_patient":
            continue
        for code in p["missing_patient"]:
            if code in seen or (only_codes is not None and code not in only_codes):
                continue
            seen.add(code)
            own = p.get("patient_reason") if len(p["doc_types"]) == 1 else ""
            reason = (own or WHY.get(code, "the insurer needs this to process your claim")).rstrip(".")
            if code == "patient_declaration":
                asks.append(f"{_declaration_line(p['text'])}. Why: {WHY['patient_declaration']}.")
            else:
                note = " (the earlier copy was not accepted, please send a clear new one)" if p.get("fresh_copy_needed") else ""
                asks.append(f"{label(code)}{note}. Why: {reason}.")
    return asks


def _fields(patient, asks, already, deadline_str):
    return {
        "patient_first_name": patient["name"].split()[0], "patient_name": patient["name"],
        "patient_id": patient["id"], "claim_no": patient.get("claim_no") or "-",
        "insurer": patient.get("insurer") or "your insurer", "hospital": patient.get("hospital") or "the hospital",
        "items": "\n".join(f"{i}. {a}" for i, a in enumerate(asks, 1)), "item_count": len(asks),
        "already_have": already, "deadline": deadline_str,
        "support_phone": os.getenv("SUPPORT_PHONE", "+91-80-0000-0000"),
        "brand": os.getenv("BRAND_NAME", "care.fi"),
    }


def compose(patient: dict, points: list[dict], deadline_str: str) -> tuple[str, str, int]:
    asks = ask_lines(points)
    if not asks:
        return "", "", 0
    in_locker = sorted({label(c).lower() for p in points for c in p["present"] if c in PATIENT_SOURCED})
    already = ""
    if in_locker:
        listed = in_locker[0] if len(in_locker) == 1 else ", ".join(in_locker[:-1]) + " and " + in_locker[-1]
        already = f"We already have your {listed}, so you don't need to send {'it' if len(in_locker) == 1 else 'them'} again.\n"
    subject, tpl = load_template(TEMPLATE_PATH)
    f = _fields(patient, asks, already, deadline_str)
    return subject.format(**f), re.sub(r"\n{3,}", "\n\n", tpl.format(**f)), len(asks)


# ---- step 4: send ----------------------------------------------------------------
def send_to_patient(patient: dict, query_id: int | None, subject: str, body: str, kind: str,
                    channels=("whatsapp", "email")) -> list[str]:
    """Sends on both channels in parallel and logs each. Skips exact repeats for the same query."""
    statuses = []
    jobs = {}
    with ThreadPoolExecutor(2) as pool:
        for ch in channels:
            dup = query_id and db.q1("""SELECT id FROM messages WHERE query_id=? AND channel=? AND body=?
                                        AND status IN ('sent','delivered','read','simulated')""", (query_id, ch, body))
            if dup:
                statuses.append("duplicate_skipped")
                continue
            jobs[ch] = pool.submit(send_whatsapp, patient["phone"], body) if ch == "whatsapp" \
                else pool.submit(send_email, patient["email"], subject, body)
        for ch, fut in jobs.items():
            st, ref, err, actual_to = fut.result()
            db.ex("""INSERT INTO messages(patient_id, query_id, direction, kind, channel, to_addr, subject, body,
                     status, provider_ref, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (patient["id"], query_id, "out", kind, ch, actual_to, subject if ch == "email" else None,
                   body, st, ref, err, db.now()))
            statuses.append(st)
    return statuses


# ---- orchestration -------------------------------------------------------------
def process_query(query_id: int):
    qrow = db.q1("SELECT * FROM queries WHERE id=?", (query_id,))
    if not qrow:
        return
    db.ex("UPDATE queries SET status='processing', error=NULL WHERE id=?", (query_id,))
    try:
        patient = db.q1("SELECT * FROM patients WHERE id=?", (qrow["patient_id"],))
        text = read_text(qrow["stored_path"])
        file_bytes = Path(qrow["stored_path"]).read_bytes()

        # 1. identify every point
        result, engine = extract_points(text, file_bytes, qrow["filename"])
        # 2. check the locker (only documents that existed when the letter arrived)
        locker = db.q("SELECT * FROM documents WHERE patient_id=? AND uploaded_at <= ?",
                      (patient["id"], qrow["uploaded_at"]))
        decided = decide(result.get("points", []), locker)

        db.ex("DELETE FROM query_points WHERE query_id=?", (query_id,))
        for i, p in enumerate(decided, 1):
            db.ex("""INSERT INTO query_points(query_id, idx, text, kind, doc_types, decision, matched_doc_ids,
                     patient_reason, fresh_copy_needed, missing_codes, present_codes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (query_id, i, p["text"], p["kind"], json.dumps(p["doc_types"]), p["decision"],
                   json.dumps(p["matched_doc_ids"]), p.get("patient_reason", ""), int(bool(p.get("fresh_copy_needed"))),
                   json.dumps(p["missing_patient"]), json.dumps(p["present"])))
        due = deadline_date(result.get("deadline", ""))
        db.ex("UPDATE queries SET raw_text=?, engine=?, summary=?, processed_at=?, deadline_at=? WHERE id=?",
              (text[:20000], engine, result.get("summary", ""), db.now(), due.strftime("%Y-%m-%d"), query_id))

        notify_for_query(query_id)
    except Exception as e:
        db.ex("UPDATE queries SET status='failed', error=? WHERE id=?",
              (f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", query_id))


def notify_for_query(query_id: int, force: bool = False):
    """3. compose + 4. send. Also used after an agent overrides a decision."""
    qrow = db.q1("SELECT * FROM queries WHERE id=?", (query_id,))
    patient = db.q1("SELECT * FROM patients WHERE id=?", (qrow["patient_id"],))
    points = point_rows(query_id)
    due = datetime.strptime(qrow["deadline_at"], "%Y-%m-%d") if qrow["deadline_at"] else deadline_date("")
    subject, body, n = compose(patient, points, due.strftime("%d %b %Y"))
    needs_review = any(p["decision"] == "needs_review" for p in points)
    if n == 0:
        db.ex("UPDATE queries SET status=? WHERE id=?",
              ("needs_review" if needs_review else "no_patient_action", query_id))
        return
    statuses = send_to_patient(patient, query_id, subject, body, "query")
    if statuses and all(s == "failed" for s in statuses):
        status = "send_failed"
    else:
        status = "sent_needs_review" if needs_review else "sent"
    db.ex("UPDATE queries SET status=?, sent_at=COALESCE(?, sent_at) WHERE id=?",
          (status, db.now() if any(s in ("sent", "simulated") for s in statuses) else None, query_id))
    refresh_progress(patient["id"])


# ---- progress: what's still pending ------------------------------------------------
def pending_for_query(qrow: dict) -> tuple[list[str], list[str]]:
    """(received_codes, pending_codes) for things we asked this patient for."""
    asked = []
    for p in point_rows(qrow["id"]):
        if p["decision"] == "need_from_patient":
            asked += [c for c in p["missing_patient"] if c not in asked]
    got = {d["doc_type"] for d in db.q("SELECT doc_type FROM documents WHERE patient_id=? AND uploaded_at > ?",
                                        (qrow["patient_id"], qrow["uploaded_at"]))}
    return [c for c in asked if c in got], [c for c in asked if c not in got]


def refresh_progress(patient_id: str):
    for qrow in db.q(f"SELECT * FROM queries WHERE patient_id=? AND status IN ({','.join('?' * len(OPEN_STATUSES))})",
                     (patient_id, *OPEN_STATUSES)):
        received, pending = pending_for_query(qrow)
        if received and not pending:
            db.ex("UPDATE queries SET status='complete', completed_at=? WHERE id=?", (db.now(), qrow["id"]))
        elif received:
            db.ex("UPDATE queries SET status='partially_received' WHERE id=?", (qrow["id"],))


def all_pending(patient_id: str) -> list[tuple[dict, list[str]]]:
    out = []
    for qrow in db.q(f"SELECT * FROM queries WHERE patient_id=? AND status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id",
                     (patient_id, *OPEN_STATUSES)):
        _, pending = pending_for_query(qrow)
        if pending:
            out.append((qrow, pending))
    return out


def send_reminder(query_id: int, by: str = "scheduler") -> bool:
    qrow = db.q1("SELECT * FROM queries WHERE id=?", (query_id,))
    _, pending = pending_for_query(qrow)
    if not pending:
        return False
    patient = db.q1("SELECT * FROM patients WHERE id=?", (qrow["patient_id"],))
    asks = ask_lines(point_rows(query_id), set(pending))
    subject, tpl = load_template(REMINDER_PATH)
    due = datetime.strptime(qrow["deadline_at"], "%Y-%m-%d").strftime("%d %b %Y") if qrow["deadline_at"] else "soon"
    f = _fields(patient, asks, "", due)
    send_to_patient(patient, query_id, subject.format(**f), re.sub(r"\n{3,}", "\n\n", tpl.format(**f)), "reminder")
    db.ex("UPDATE queries SET reminder_sent_at=? WHERE id=?", (db.now(), query_id))
    db.audit(by, "REMINDER", f"{patient['id']}:q{query_id}")
    return True


def run_due_reminders():
    hours = int(os.getenv("REMINDER_HOURS_BEFORE", "24"))
    cutoff = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d")
    for qrow in db.q(f"""SELECT id FROM queries WHERE reminder_sent_at IS NULL AND deadline_at <= ?
                        AND status IN ({','.join('?' * len(OPEN_STATUSES))})""", (cutoff, *OPEN_STATUSES)):
        send_reminder(qrow["id"])
