"""
Google's OAuth flow round-trips a 'state' string through the browser.
Since the server keeps no session store, we sign the state ourselves
(purely for CSRF protection) instead of comparing it against anything
stored server-side.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-oauth-state")
STATE_MAX_AGE = 60 * 10  # 10 minutes to complete the OAuth round trip


def make_state(purpose: str) -> str:
    return _serializer.dumps({"purpose": purpose})


def verify_state(state: str, expected_purpose: str) -> bool:
    try:
        payload = _serializer.loads(state, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return payload.get("purpose") == expected_purpose
