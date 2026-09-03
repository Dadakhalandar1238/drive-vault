"""
Tests the pieces that make "never store the user's Client ID/Secret on
our server" actually true: the pending-setup cookie (survives only the
OAuth round trip), the session cookie carrying per-user credentials, and
the vault's encrypted backup of those same credentials.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app import session, vault  # noqa: E402


def test_pending_setup_cookie_roundtrip():
    cookie = session.create_pending_setup_cookie("me@example.com", "my-client-id", "my-client-secret")
    data = session.read_pending_setup_cookie(cookie)
    assert data["email"] == "me@example.com"
    assert data["client_id"] == "my-client-id"
    assert data["client_secret"] == "my-client-secret"


def test_pending_setup_cookie_does_not_store_secret_in_plaintext():
    cookie = session.create_pending_setup_cookie("me@example.com", "my-client-id", "super-secret-value")
    # The raw cookie string must never contain the plaintext secret --
    # only the decrypted round trip should reveal it.
    assert "super-secret-value" not in cookie


def test_pending_setup_cookie_rejects_garbage():
    assert session.read_pending_setup_cookie("not-a-real-cookie") is None
    assert session.read_pending_setup_cookie(None) is None


def test_session_cookie_carries_client_credentials_encrypted():
    cookie = session.create_session_cookie(
        "sub123", "me@example.com", "refresh-token-xyz", "my-client-id", "top-secret-value",
    )
    assert "top-secret-value" not in cookie  # not plaintext in the cookie
    data = session.read_session_cookie(cookie)
    assert data["client_id"] == "my-client-id"
    assert data["client_secret"] == "top-secret-value"


def test_session_cookie_from_before_per_user_oauth_is_treated_as_expired():
    # Simulates a cookie created by the OLD single-shared-app version of
    # this code, which had no client_id/cs fields. Must not crash --
    # just forces a clean re-login instead.
    from itsdangerous import URLSafeTimedSerializer
    from app import config

    old_style_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-session")
    old_cookie = old_style_serializer.dumps({"sub": "x", "email": "y", "rt": "z"})
    assert session.read_session_cookie(old_cookie) is None


def test_vault_stores_oauth_client_encrypted_and_never_in_plaintext():
    v = {"accounts": {}, "folders": [], "files": {}, "oauth_client": None}
    vault.set_oauth_client(v, "my-client-id", "my-secret-value")

    # The client_secret must not appear anywhere in the vault in plaintext
    import json
    raw = json.dumps(v)
    assert "my-secret-value" not in raw
    assert "my-client-id" in raw  # client_id itself isn't sensitive

    client_id, client_secret = vault.get_oauth_client(v)
    assert client_id == "my-client-id"
    assert client_secret == "my-secret-value"


def test_vault_get_oauth_client_returns_none_when_not_set():
    v = {"accounts": {}, "folders": [], "files": {}, "oauth_client": None}
    assert vault.get_oauth_client(v) is None


def test_load_vault_migrates_vault_missing_oauth_client_key():
    import json
    from unittest.mock import MagicMock
    from app.crypto import encrypt

    old_vault_json = json.dumps({"accounts": {}, "files": {"a.txt": {"size": 1, "uploaded_at": "x", "chunks": []}}})
    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = encrypt(old_vault_json)

    loaded = vault.load_vault(fake_client)
    assert loaded["oauth_client"] is None
    assert loaded["folders"] == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
