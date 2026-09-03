"""
Tests the token cache in isolation (hit/miss/expiry/buffer logic), and
that DriveClient actually reads from and writes to it via account_key.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app import token_cache  # noqa: E402
from app.drive_client import DriveClient  # noqa: E402


def test_cache_miss_returns_none():
    assert token_cache.get("never-set-account") is None


def test_cache_set_then_get_roundtrip():
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    token_cache.set("acct-a", "token-abc", expiry)
    result = token_cache.get("acct-a")
    assert result == ("token-abc", expiry)


def test_cache_treats_near_expiry_token_as_miss():
    # Within the buffer window (90s) of expiring -- should be treated as
    # expired so we never hand out a token that dies mid-request.
    almost_expired = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)
    token_cache.set("acct-b", "token-b", almost_expired)
    assert token_cache.get("acct-b") is None


def test_cache_ignores_missing_or_none_values():
    token_cache.set("acct-c", None, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
    assert token_cache.get("acct-c") is None
    token_cache.set("acct-d", "token-d", None)
    assert token_cache.get("acct-d") is None
    token_cache.set(None, "token-e", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
    assert token_cache.get(None) is None


def test_driveclient_picks_up_cached_token_on_construction():
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    token_cache.set("acct-picked-up", "cached-access-token", expiry)

    client = DriveClient("some-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "test-client-id", "test-client-secret", account_key="acct-picked-up")
    assert client._creds.token == "cached-access-token"
    assert client._creds.expiry == expiry


def test_driveclient_without_cache_hit_starts_with_no_token():
    client = DriveClient("some-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "test-client-id", "test-client-secret", account_key="acct-never-cached")
    assert client._creds.token is None


def test_touch_cache_writes_current_creds_into_shared_cache():
    client = DriveClient("some-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "test-client-id", "test-client-secret", account_key="acct-touch")
    # Simulate what would happen after a real API call refreshed the token.
    client._creds.token = "freshly-refreshed-token"
    client._creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    client._touch_cache()

    cached = token_cache.get("acct-touch")
    assert cached is not None
    assert cached[0] == "freshly-refreshed-token"


def test_fresh_copy_reuses_whatever_is_currently_cached():
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    token_cache.set("acct-shared", "shared-valid-token", expiry)

    parent = DriveClient("rt", ["scope"], "test-client-id", "test-client-secret", account_key="acct-shared")
    child = parent.fresh_copy()

    # Both should have picked up the same cached token instead of each
    # starting from scratch (which would mean a redundant refresh each).
    assert parent._creds.token == "shared-valid-token"
    assert child._creds.token == "shared-valid-token"


def test_client_without_account_key_never_touches_shared_cache():
    # Ad-hoc/test clients with no account_key must not blow up when
    # _touch_cache() is called, and must not pollute the cache under a
    # None key.
    client = DriveClient("rt", ["scope"], "test-client-id", "test-client-secret")  # account_key defaults to None
    client._creds.token = "irrelevant"
    client._creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    client._touch_cache()  # should not raise
    assert token_cache.get(None) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
