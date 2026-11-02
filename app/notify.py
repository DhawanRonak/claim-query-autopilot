"""Outbound WhatsApp + email. If a channel is not configured the message is
recorded as 'simulated' so the rest of the flow can still be demoed."""
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx


def _wa_number(phone: str) -> str:
    p = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    if not p.startswith("+"):
        p = "+91" + p[-10:] if len(p) >= 10 else p
    return p


def whatsapp_provider() -> str:
    if os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"):
        return "twilio"
    if os.getenv("META_WA_TOKEN") and os.getenv("META_WA_PHONE_NUMBER_ID"):
        return "meta"
    return "simulated"


def send_whatsapp(phone: str, body: str) -> tuple[str, str, str, str]:
    """Returns (status, provider_ref, error, number_actually_used)."""
    to = os.getenv("WHATSAPP_OVERRIDE_TO") or phone   # demo safety: route all sends to one test number
    to = _wa_number(to)
    st, ref, err = _send_wa(to, body)
    return st, ref, err, to


def _send_wa(to: str, body: str) -> tuple[str, str, str]:
    body = body if len(body) <= 1550 else body[:1500] + "\n…(full list sent on email)"
    prov = whatsapp_provider()
    try:
        if prov == "twilio":
            sid = os.environ["TWILIO_ACCOUNT_SID"]
            data = {"From": os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
                    "To": f"whatsapp:{to}", "Body": body}
            if os.getenv("PUBLIC_BASE_URL"):   # delivery / read receipts
                data["StatusCallback"] = os.getenv("PUBLIC_BASE_URL").rstrip("/") + "/webhooks/twilio/status"
            r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                           auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]), timeout=20, data=data)
            if r.status_code >= 300:
                return "failed", "", f"Twilio {r.status_code}: {r.text[:300]}"
            return "sent", r.json().get("sid", ""), ""
        if prov == "meta":
            r = httpx.post(f"https://graph.facebook.com/v21.0/{os.environ['META_WA_PHONE_NUMBER_ID']}/messages",
                           headers={"Authorization": f"Bearer {os.environ['META_WA_TOKEN']}"}, timeout=20,
                           json={"messaging_product": "whatsapp", "to": to.lstrip("+"),
                                 "type": "text", "text": {"body": body}})
            if r.status_code >= 300:
                return "failed", "", f"Meta {r.status_code}: {r.text[:300]}"
            return "sent", (r.json().get("messages") or [{}])[0].get("id", ""), ""
        return "simulated", "", "WhatsApp not configured (see .env)"
    except Exception as e:
        return "failed", "", f"{type(e).__name__}: {e}"


def send_email(to_addr: str, subject: str, body: str) -> tuple[str, str, str, str]:
    to = os.getenv("EMAIL_OVERRIDE_TO") or to_addr
    st, ref, err = _send_email(to, subject, body)
    return st, ref, err, to


def _send_email(to: str, subject: str, body: str) -> tuple[str, str, str]:
    host, user, pw = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if not (host and user and pw):
        return "simulated", "", "Email not configured (see .env)"
    try:
        msg = EmailMessage()
        msg["From"] = os.getenv("EMAIL_FROM", user)
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
            s.send_message(msg)
        return "sent", msg.get("Message-ID", "") or "", ""
    except Exception as e:
        return "failed", "", f"{type(e).__name__}: {e}"


def email_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def download_twilio_media(url: str) -> bytes:
    sid, tok = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    r = httpx.get(url, auth=(sid, tok) if sid else None, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.content


def twilio_signature_ok(url: str, params: dict, signature: str) -> bool:
    import base64, hashlib, hmac
    tok = os.getenv("TWILIO_AUTH_TOKEN")
    if not tok:
        return True
    payload = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(tok.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature or "")
