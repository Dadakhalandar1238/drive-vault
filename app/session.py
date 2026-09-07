"""
The server keeps NO session state anywhere, and it never persists a
user's Google OAuth Client ID/Secret to disk or a database. Everything
needed to act as the user -- their google id, email, refresh token, and
their OWN Client ID/Secret -- is packed into a signed, encrypted cookie.
This is what lets the app run on a free tier that spins instances up/down
freely: any instance can serve any request just from the cookie.

Three cookies exist:
  - the SESSION cookie: long-lived (config.SESSION_MAX_AGE), used once
    setup is complete.
  - the PENDING SETUP cookie: short-lived, holds a user's freshly-entered
    Client ID/Secret only long enough to survive the redirect out to
    Google and back during first-time setup. Discarded immediately after.
  - the REMEMBER cookie: separate from the session, much longer-lived
    (config.REMEMBER_MAX_AGE), holds the same Client ID/Secret *and* the
    refresh token from the last successful login. Its purpose is to
    survive the session cookie expiring or being cleared -- but unlike a
    plain "skip the form" shortcut, /continue uses the stored refresh
    token to resume the existing Google grant directly, with no redirect
    to Google at all, so a returning user isn't shown the consent screen
    again for something they already approved. Falls back to a real
    Google round trip only if that stored refresh token turns out to no
    longer be valid (revoked, or unused long enough to expire). Set (and
    refreshed) every time a login actually succeeds in /oauth/callback.

Client Secret (and refresh tokens) are individually encrypted within the
cookie payload, not just signed -- signing alone stops tampering but
doesn't stop someone reading the value straight out of the cookie.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config
from .crypto import decrypt, encrypt

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-session")
_pending_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-pending-setup")
_remember_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-remember")

# How long a user has to finish the Google consent screen after submitting
# their Client ID/Secret before they'd need to re-enter them.
PENDING_SETUP_MAX_AGE = 60 * 15


def create_session_cookie(google_sub: str, email: str, refresh_token: str, client_id: str, client_secret: str) -> str:
    payload = {
        "sub": google_sub,
        "email": email,
        "rt": encrypt(refresh_token),
        "client_id": client_id,  # not secret -- OAuth client IDs are meant to be public
        "cs": encrypt(client_secret),
    }
    return _serializer.dumps(payload)


def read_session_cookie(cookie_value: str | None) -> dict | None:
    if not cookie_value:
        return None
    try:
        payload = _serializer.loads(cookie_value, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if "client_id" not in payload or "cs" not in payload:
        # Cookie from before per-user OAuth apps existed -- treat as
        # expired rather than crashing; the user just signs in again.
        return None
    payload["refresh_token"] = decrypt(payload["rt"])
    payload["client_secret"] = decrypt(payload["cs"])
    return payload


def create_pending_setup_cookie(email: str, client_id: str, client_secret: str) -> str:
    payload = {"email": email, "client_id": client_id, "cs": encrypt(client_secret)}
    return _pending_serializer.dumps(payload)


def read_pending_setup_cookie(cookie_value: str | None) -> dict | None:
    if not cookie_value:
        return None
    try:
        payload = _pending_serializer.loads(cookie_value, max_age=PENDING_SETUP_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    payload["client_secret"] = decrypt(payload["cs"])
    return payload


def create_remember_cookie(sub: str, email: str, client_id: str, client_secret: str, refresh_token: str) -> str:
    """The refresh token is what makes /continue a genuinely silent
    resume instead of just a shortcut past the credential form -- Google
    refresh tokens don't expire or rotate on use, so the SAME one from
    the last real login can be reused indefinitely, with no redirect to
    Google's consent screen at all, as long as it's still valid."""
    payload = {
        "sub": sub, "email": email, "client_id": client_id,
        "cs": encrypt(client_secret), "rt": encrypt(refresh_token),
    }
    return _remember_serializer.dumps(payload)


def read_remember_cookie(cookie_value: str | None) -> dict | None:
    if not cookie_value:
        return None
    try:
        payload = _remember_serializer.loads(cookie_value, max_age=config.REMEMBER_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    payload["client_secret"] = decrypt(payload["cs"])
    # "rt"/"sub" are absent in a cookie saved before refresh tokens were
    # remembered here -- treat as present-but-unusable rather than crash;
    # /continue falls back to a full Google round trip in that case.
    payload["refresh_token"] = decrypt(payload["rt"]) if "rt" in payload else None
    payload.setdefault("sub", None)
    return payload
