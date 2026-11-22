"""Personal sign-in links for patients: a signed, expiring token, so messages never carry a password."""
import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_env = os.getenv("SECRET_KEY")
SECRET = _env if _env not in (None, "", "change-me-to-a-long-random-string") else secrets.token_hex(32)
LINK_DAYS = int(os.getenv("LINK_VALID_DAYS", "14"))
_ser = URLSafeTimedSerializer(SECRET, salt="patient-open-link")


def make_token(patient_id: str) -> str:
    return _ser.dumps(patient_id)


def read_token(token: str) -> str | None:
    """Patient ID if the link is genuine and not expired, else None."""
    try:
        return _ser.loads(token, max_age=LINK_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None
