"""Patient replies on WhatsApp: files go straight into their locker, pending items
tick off, and the patient gets an automatic reply listing what is still pending."""
import re

from . import db
from .notify import _send_wa, _wa_number, download_twilio_media
from .pipeline import add_document, all_pending
from .taxonomy import label

EXT = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
       "image/webp": ".jpg", "text/plain": ".txt"}


def _digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())[-10:]


def resolve_patient(sender: str, body: str) -> dict | None:
    d = _digits(sender)
    # 1) a patient ID typed in the message, if we've messaged this number about that patient
    m = re.search(r"\bCF-P\d{3,}\b", body or "", re.I)
    if m:
        p = db.q1("SELECT * FROM patients WHERE upper(id)=?", (m.group(0).upper(),))
        if p and (_digits(p["phone"]) == d or db.q1(
                "SELECT 1 FROM messages WHERE patient_id=? AND channel='whatsapp' AND direction='out' AND to_addr LIKE ?",
                (p["id"], f"%{d}"))):
            return p
    # 2) the patient's own registered number
    pats = [p for p in db.q("SELECT * FROM patients") if _digits(p["phone"]) == d]
    if len(pats) == 1:
        return pats[0]
    # 3) whoever we last messaged on this number (test numbers shared across patients)
    last = db.q1("""SELECT patient_id FROM messages WHERE channel='whatsapp' AND direction='out'
                    AND to_addr LIKE ? ORDER BY id DESC LIMIT 1""", (f"%{d}",))
    return db.q1("SELECT * FROM patients WHERE id=?", (last["patient_id"],)) if last else None


def handle_whatsapp(form: dict):
    sender = form.get("From", "").replace("whatsapp:", "")
    body = (form.get("Body") or "").strip()
    n = int(form.get("NumMedia") or 0)
    patient = resolve_patient(sender, body)
    if not patient:
        db.audit("whatsapp", "INBOUND_UNMATCHED", _wa_number(sender)[-4:])
        _send_wa(_wa_number(sender), "Hi! We couldn't match this number to a claim. Please reply with your "
                                     "Patient ID (e.g. CF-P001) from our earlier message.")
        return

    db.ex("""INSERT INTO messages(patient_id, direction, kind, channel, to_addr, body, status, provider_ref, created_at)
             VALUES (?,?,?,?,?,?,?,?,?)""",
          (patient["id"], "in", "reply", "whatsapp", _wa_number(sender),
           (body + (f"\n[{n} attachment(s)]" if n else "")).strip(), "received", form.get("MessageSid", ""), db.now()))

    received, notes = [], []
    for i in range(n):
        url, ctype = form.get(f"MediaUrl{i}"), (form.get(f"MediaContentType{i}") or "").lower()
        ext = EXT.get(ctype)
        if not url or not ext:
            notes.append("One file type wasn't supported. Please send PDFs or photos.")
            continue
        try:
            data = download_twilio_media(url)
        except Exception as e:
            notes.append("We couldn't download one file. Please send it again.")
            db.audit("whatsapp", "MEDIA_FAILED", f"{patient['id']}:{type(e).__name__}")
            continue
        # a duplicate of something already in the locker (same bytes) is not stored twice
        doc = add_document(patient["id"], f"whatsapp_{form.get('MessageSid', 'msg')[-6:]}_{i + 1}{ext}", data,
                           by="patient (WhatsApp)", source="whatsapp", notes=body or None)
        received.append(doc)

    # a typed explanation counts as the written note, if one is pending
    pend = all_pending(patient["id"])
    if not n and len(body) >= 25 and any("patient_declaration" in codes for _, codes in pend):
        doc = add_document(patient["id"], "whatsapp_note.txt", body.encode(), by="patient (WhatsApp)",
                           source="whatsapp", doc_type="patient_declaration")
        received.append(doc)
        pend = all_pending(patient["id"])

    _send_wa(_wa_number(sender), reply_text(patient, received, pend, notes))


def reply_text(patient, received, pend, notes) -> str:
    first = patient["name"].split()[0]
    lines = []
    if received:
        lines.append(f"Thanks {first}, we've received:")
        for d in received:
            lines.append(f"• {label(d['doc_type'])}" + (" (already had this one, no need to resend)" if d.get("duplicate") else ""))
            if d.get("flag") and "Name on document" in d["flag"]:
                lines.append("  (this seems to have a different name on it, our team will check it)")
            elif d.get("flag") and "Hard to read" in d["flag"]:
                lines.append("  (this is hard to read, please send a clearer photo)")
    else:
        lines.append(f"Hi {first}, thanks for your message.")
    lines += notes
    still = []
    for _, codes in pend:
        still += [c for c in codes if c not in still]
    if still:
        lines.append("\nStill pending:")
        lines += [f"• {label(c)}" for c in still]
        lines.append("\nPlease send these as clear photos or PDFs here.")
    else:
        lines.append(f"\nThat's everything we need for now. We'll send it to {patient.get('insurer') or 'your insurer'} and keep you posted.")
    return "\n".join(lines)


META_MIME = {"image": "image/jpeg", "document": "application/pdf"}


def handle_meta(payload: dict):
    """Meta WhatsApp Cloud API webhook: incoming messages and delivery receipts."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            for st in v.get("statuses", []):
                status = {"delivered": "delivered", "read": "read", "failed": "failed"}.get(st.get("status"))
                if status:
                    err = (st.get("errors") or [{}])[0]
                    db.ex("UPDATE messages SET status=?, error=COALESCE(?, error) WHERE provider_ref=?",
                          (status, err.get("title") or err.get("message"), st.get("id")))
            for m in v.get("messages", []):
                t = m.get("type")
                form = {"From": "+" + m.get("from", ""), "MessageSid": m.get("id", ""), "NumMedia": "0", "Body": ""}
                if t == "text":
                    form["Body"] = m.get("text", {}).get("body", "")
                elif t in ("image", "document"):
                    media = m.get(t, {})
                    form.update({"NumMedia": "1", "MediaUrl0": "meta:" + media.get("id", ""),
                                 "MediaContentType0": (media.get("mime_type") or META_MIME[t]).split(";")[0],
                                 "Body": media.get("caption", "")})
                elif t == "button":
                    form["Body"] = m.get("button", {}).get("text", "")
                else:
                    form["Body"] = f"[{t} message]"
                handle_whatsapp(form)
