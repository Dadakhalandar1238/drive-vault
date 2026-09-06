"""
The server keeps NO session state anywhere. Everything needed to act as
the user -- their google id, email, and refresh token -- is packed into a
signed, encrypted cookie. This is what lets the app run on a free tier
that spins instances up/down freely: any instance can serve any request
just from the cookie.

The refresh token is encrypted within the cookie payload, not just signed
-- signing alone stops tampering but doesn't stop someone reading the
value straight out of the cookie.

There's no separate "pending setup" or "remember me" cookie here (unlike
earlier versions of this app): since every user signs in through the one
shared Google OAuth app configured in config.py, there's no per-user
credential that ever needs to survive a redirect round trip or be
remembered across a lost session -- signing in again is always just a
single "Sign in with Google" click.
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
    if "rt" not in payload:
        # Cookie from an earlier version of this app -- treat as expired
        # rather than crashing; the user just signs in again.
        return None
    payload["refresh_token"] = decrypt(payload["rt"])
    return payload
