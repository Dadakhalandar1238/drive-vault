"""
Caches short-lived Google OAuth access tokens in memory, keyed by the
Google account's own id (its "sub"). This is purely a latency
optimization -- refresh_token is what's actually authoritative, and that
always still lives in the session cookie / vault. Losing this cache (a
restart, or another server instance on a scaled-up deploy) just costs one
extra token refresh on the next request; nothing goes stale in a way
that matters.

Without this, every request rebuilt DriveClient objects from scratch and
therefore re-exchanged the refresh token for a brand new access token
before doing any real Drive API work -- once per connected account, on
every single request, even though access tokens last about an hour.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_tokens: dict[str, tuple[str, datetime]] = {}

# Treat a token as expired a bit early so we never hand out one that dies
# mid-request.
_EXPIRY_BUFFER = timedelta(seconds=90)


def _utcnow() -> datetime:
    # Matches google-auth's own internal helper: naive UTC (no tzinfo),
    # since that's what Credentials.expiry is set to after a real refresh,
    # and naive/aware datetimes can't be compared against each other.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get(key: str) -> tuple[str, datetime] | None:
    if not key:
        return None
    with _lock:
        entry = _tokens.get(key)
    if entry is None:
        return None
    token, expiry = entry
    if expiry is None or _utcnow() >= expiry - _EXPIRY_BUFFER:
        return None
    return entry


def set(key: str, token: str | None, expiry: datetime | None) -> None:
    if not key or not token or not expiry:
        return
    with _lock:
        _tokens[key] = (token, expiry)
