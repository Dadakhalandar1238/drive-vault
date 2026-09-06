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


def test_load_vault_self_heals_when_encrypted_under_a_different_secret_key():
    """Reproduces the exact reported crash: a vault written by a different
    SECRET_KEY (e.g. local testing vs. a deployed instance with its own
    auto-generated key) must not crash the whole request -- it should
    start a fresh vault instead, and back up the undecryptable original."""
    import base64
    import hashlib
    import json as jsonlib
    from unittest.mock import MagicMock

    from cryptography.fernet import Fernet

    different_key_digest = hashlib.sha256(b"a-totally-different-secret-key").digest()
    different_fernet = Fernet(base64.urlsafe_b64encode(different_key_digest))
    ciphertext_from_other_key = different_fernet.encrypt(
        jsonlib.dumps({"accounts": {}, "files": {"old.txt": {}}}).encode()
    ).decode()

    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = ciphertext_from_other_key

    loaded = vault.load_vault(fake_client)  # must not raise
    assert loaded["files"] == {}
    assert loaded["accounts"] == {}
    assert loaded["folders"] == []
    assert loaded["oauth_client"] is None

    # The undecryptable original must be preserved, not silently discarded.
    fake_client.write_backup_blob.assert_called_once()
    backup_name, backup_content = fake_client.write_backup_blob.call_args[0]
    assert backup_name.startswith("vault.enc.backup-")
    assert backup_content == ciphertext_from_other_key


def test_load_vault_self_heals_on_corrupted_garbage_content():
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = "not-even-valid-fernet-ciphertext"

    loaded = vault.load_vault(fake_client)  # must not raise
    assert loaded == vault.EMPTY_VAULT or loaded["files"] == {}
    fake_client.write_backup_blob.assert_called_once()


def test_remember_cookie_roundtrip():
    cookie = session.create_remember_cookie("me@example.com", "my-client-id", "my-client-secret")
    data = session.read_remember_cookie(cookie)
    assert data["email"] == "me@example.com"
    assert data["client_id"] == "my-client-id"
    assert data["client_secret"] == "my-client-secret"


def test_remember_cookie_does_not_store_secret_in_plaintext():
    cookie = session.create_remember_cookie("me@example.com", "my-client-id", "super-secret-value")
    assert "super-secret-value" not in cookie


def test_remember_cookie_rejects_garbage():
    assert session.read_remember_cookie("not-a-real-cookie") is None
    assert session.read_remember_cookie(None) is None


def test_load_vault_proceeds_even_if_backup_write_itself_fails():
    """Losing the backup is better than crashing the login entirely."""
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = "garbage-ciphertext"
    fake_client.write_backup_blob.side_effect = RuntimeError("Drive API quota exceeded")

    loaded = vault.load_vault(fake_client)  # must not raise, even though backup failed
    assert loaded["files"] == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
