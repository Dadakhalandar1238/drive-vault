"""
The server keeps NO session state anywhere. Everything needed to act as
the user -- their google id, email, and (encrypted) primary refresh token
-- is packed into a signed, tamper-proof cookie. This is what lets the
app run on a free tier that spins instances up/down freely: any instance
can serve any request just from the cookie.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config
from .crypto import decrypt, encrypt

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-session")


def create_session_cookie(google_sub: str, email: str, refresh_token: str) -> str:
    payload = {
        "sub": google_sub,
        "email": email,
        "rt": encrypt(refresh_token),
    }
    return _serializer.dumps(payload)


def read_session_cookie(cookie_value: str | None) -> dict | None:
    if not cookie_value:
        return None
    try:
        payload = _serializer.loads(cookie_value, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    payload["refresh_token"] = decrypt(payload["rt"])
    return payload
