import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "files"          # private: never mounted as static
DB_PATH = DATA_DIR / "locker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','agent','patient')),
    display_name TEXT,
    patient_id TEXT              -- set only for role = patient
);
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,         -- patient ID, e.g. CF-P001
    name TEXT NOT NULL,
    phone TEXT, email TEXT,
    insurer TEXT, policy_no TEXT, claim_no TEXT,
    hospital TEXT, admission_date TEXT, diagnosis TEXT,
    assigned_agent TEXT,         -- username of the agent who owns the case
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    doc_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_at TEXT, uploaded_by TEXT,
    notes TEXT,
    confidence REAL,             -- classifier confidence 0..1
    classified_by TEXT,          -- ai engine | filename | agent
    flag TEXT,                   -- e.g. 'name mismatch: Ramesh K'
    source TEXT DEFAULT 'portal', -- portal | whatsapp | import
    sha256 TEXT                  -- exact-duplicate detection
);
CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_at TEXT, uploaded_by TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    engine TEXT,
    raw_text TEXT,
    summary TEXT,
    processed_at TEXT, sent_at TEXT,
    deadline_at TEXT,            -- YYYY-MM-DD we asked the patient to reply by
    reminder_sent_at TEXT,
    completed_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS query_points (
    id INTEGER PRIMARY KEY,
    query_id INTEGER NOT NULL REFERENCES queries(id),
    idx INTEGER,
    text TEXT,
    kind TEXT,                   -- document | clarification
    doc_types TEXT,              -- JSON list of taxonomy codes
    decision TEXT,               -- in_locker | need_from_patient | hospital_team | needs_review
    matched_doc_ids TEXT,        -- JSON list
    patient_reason TEXT,         -- plain-language "why" for the patient
    fresh_copy_needed INTEGER DEFAULT 0,
    missing_codes TEXT,          -- JSON: patient-held codes we asked for
    present_codes TEXT,          -- JSON: codes already in the locker
    overridden_by TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    query_id INTEGER REFERENCES queries(id),
    direction TEXT DEFAULT 'out',-- out | in
    kind TEXT DEFAULT 'query',   -- query | reminder | reply | test
    channel TEXT NOT NULL,       -- whatsapp | email
    to_addr TEXT,
    subject TEXT,
    body TEXT,
    status TEXT,                 -- sent | delivered | read | failed | simulated | received
    provider_ref TEXT,
    error TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT, username TEXT, action TEXT, target TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def audit(username: str, action: str, target: str = ""):
    ex("INSERT INTO audit_log(at, username, action, target) VALUES (?,?,?,?)",
       (now(), username, action, target))


# ---- passwords ---------------------------------------------------------------
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split("$", 1)
    return secrets.compare_digest(
        hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def jload(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except Exception:
        return default if default is not None else []
