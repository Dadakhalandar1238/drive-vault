"""
Tests the session cookie (now just sub/email/refresh_token, since Client
ID/Secret are shared server config rather than a per-user credential) and
the vault's self-healing behavior when it can't be decrypted.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "unit-test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "unit-test-client-secret")

from app import session, vault  # noqa: E402


def test_session_cookie_carries_refresh_token_encrypted():
    cookie = session.create_session_cookie("sub123", "me@example.com", "top-secret-refresh-token")
    assert "top-secret-refresh-token" not in cookie  # not plaintext in the cookie
    data = session.read_session_cookie(cookie)
    assert data["sub"] == "sub123"
    assert data["email"] == "me@example.com"
    assert data["refresh_token"] == "top-secret-refresh-token"


def test_session_cookie_from_before_this_format_is_treated_as_expired():
    # Simulates a cookie created by an earlier version of this app with a
    # different payload shape. Must not crash -- just forces a clean
    # re-login instead.
    from itsdangerous import URLSafeTimedSerializer
    from app import config

    old_style_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="dv-session")
    old_cookie = old_style_serializer.dumps({"sub": "x", "email": "y"})  # no "rt"
    assert session.read_session_cookie(old_cookie) is None


def test_load_vault_migrates_vault_missing_folders_key():
    import json
    from unittest.mock import MagicMock
    from app.crypto import encrypt

    old_vault_json = json.dumps({"accounts": {}, "files": {"a.txt": {"size": 1, "uploaded_at": "x", "chunks": []}}})
    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = encrypt(old_vault_json)

    loaded = vault.load_vault(fake_client)
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
