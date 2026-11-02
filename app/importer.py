"""Bulk import: a ZIP (or a single CSV/XLSX/JSON) of patient profiles and their documents.

Accepted ZIP layouts (mix freely):
  patients.csv | patients.xlsx | patients.json      one row per patient
  <PATIENT_ID>/<any files>                          documents for that patient
  <Patient Name>/<any files>                        also matched, by name
  <PATIENT_ID>_<anything>.pdf                       loose files, matched by prefix
Files under a folder called queries/ (or with 'query' in the name) are treated as
insurer query letters and processed straight away.
"""
import csv
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

from . import db

ALIASES = {
    "id": ["patient_id", "patientid", "id", "patient id", "uhid", "mrn"],
    "name": ["name", "patient_name", "patient name", "full_name", "full name"],
    "phone": ["phone", "mobile", "phone_number", "mobile_number", "whatsapp", "contact"],
    "email": ["email", "email_id", "mail", "email address"],
    "insurer": ["insurer", "insurance_company", "insurance company", "tpa", "payer"],
    "policy_no": ["policy_no", "policy_number", "policy number", "policy"],
    "claim_no": ["claim_no", "claim_number", "claim number", "claim_id", "claim"],
    "hospital": ["hospital", "hospital_name", "provider"],
    "admission_date": ["admission_date", "doa", "date_of_admission", "admitted_on", "admission date"],
    "diagnosis": ["diagnosis", "ailment", "condition"],
    "assigned_agent": ["assigned_agent", "agent", "owner"],
}
DOC_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}


def _norm_row(row: dict) -> dict:
    low = {str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in row.items()}
    out = {}
    for field, keys in ALIASES.items():
        for k in keys:
            if low.get(k):
                out[field] = low[k]
                break
    return out


def _rows_from(name: str, data: bytes) -> list[dict]:
    n = name.lower()
    if n.endswith(".csv"):
        return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="ignore"))))
    if n.endswith(".json"):
        j = json.loads(data)
        return j if isinstance(j, list) else j.get("patients", [])
    if n.endswith(".xlsx"):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        return [dict(zip(rows[0], r)) for r in rows[1:] if any(r)]
    return []


def upsert_patient(r: dict, by: str) -> tuple[str, bool]:
    pid = r.get("id") or "CF-P" + str(1 + len(db.q("SELECT id FROM patients"))).zfill(3)
    exists = db.q1("SELECT id FROM patients WHERE id=?", (pid,))
    cols = ["name", "phone", "email", "insurer", "policy_no", "claim_no", "hospital", "admission_date",
            "diagnosis", "assigned_agent"]
    if exists:
        sets = [c for c in cols if r.get(c)]
        if sets:
            db.ex(f"UPDATE patients SET {', '.join(c + '=?' for c in sets)} WHERE id=?", (*[r[c] for c in sets], pid))
    else:
        db.ex(f"INSERT INTO patients(id, {', '.join(cols)}, created_at) VALUES (?{', ?' * len(cols)}, ?)",
              (pid, *[r.get(c) or (pid if c == "name" else None) for c in cols], db.now()))
        uname = re.sub(r"[^a-z0-9]", "", pid.lower())
        if not db.q1("SELECT id FROM users WHERE username=?", (uname,)):
            import os
            db.ex("INSERT INTO users(username,password_hash,role,display_name,patient_id) VALUES (?,?,?,?,?)",
                  (uname, db.hash_pw(os.getenv("SEED_PATIENT_PW", "Patient@2026")), "patient", r.get("name"), pid))
    db.audit(by, "IMPORT_PATIENT", pid)
    return pid, not exists


def import_bundle(filename: str, data: bytes, by: str, process_query, add_document) -> dict:
    report = {"patients_created": 0, "patients_updated": 0, "documents": 0, "queries": 0,
              "duplicates": 0, "unmatched_files": []}
    if not filename.lower().endswith(".zip"):
        for r in _rows_from(filename, data):
            _, new = upsert_patient(_norm_row(r), by)
            report["patients_created" if new else "patients_updated"] += 1
        return report

    z = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n
             and not PurePosixPath(n).name.startswith(".")]
    for n in names:
        if PurePosixPath(n).name.lower().startswith("patients") and PurePosixPath(n).suffix.lower() in (".csv", ".json", ".xlsx"):
            for r in _rows_from(n, z.read(n)):
                _, new = upsert_patient(_norm_row(r), by)
                report["patients_created" if new else "patients_updated"] += 1

    pats = db.q("SELECT id, name FROM patients")
    by_id = {p["id"].lower(): p["id"] for p in pats}
    by_name = {re.sub(r"[^a-z]", "", p["name"].lower()): p["id"] for p in pats}

    def match(path: PurePosixPath):
        for part in path.parts[:-1]:
            k = part.lower()
            if k in by_id:
                return by_id[k]
            if re.sub(r"[^a-z]", "", k) in by_name:
                return by_name[re.sub(r"[^a-z]", "", k)]
            m = re.match(r"(cf-p\d+)", k)
            if m and m.group(1) in by_id:
                return by_id[m.group(1)]
        m = re.match(r"(cf-p\d+)", path.name.lower())
        return by_id.get(m.group(1)) if m else None

    queued = []
    for n in names:
        path = PurePosixPath(n)
        if path.suffix.lower() not in DOC_EXT or path.name.lower().startswith("patients"):
            continue
        pid = match(path)
        if not pid:
            report["unmatched_files"].append(n)
            continue
        blob = z.read(n)
        is_query = any(p.lower() in ("queries", "query", "query_letters", "query letters") for p in path.parts[:-1]) \
            or "query" in path.name.lower()
        if is_query:
            queued.append((pid, path, blob))
        else:
            d = add_document(pid, path.name, blob, by=by, source="import")
            report["duplicates" if d.get("duplicate") else "documents"] += 1
    import uuid
    for pid, path, blob in queued:   # documents first, then letters, so the locker check sees everything
        folder = db.UPLOAD_DIR / pid
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{uuid.uuid4().hex}{path.suffix.lower()}"
        target.write_bytes(blob)
        qid = db.ex("INSERT INTO queries(patient_id, filename, stored_path, uploaded_at, uploaded_by, status) VALUES (?,?,?,?,?, 'received')",
                    (pid, path.name, str(target), db.now(), by))
        report["queries"] += 1
        process_query(qid)
    return report
