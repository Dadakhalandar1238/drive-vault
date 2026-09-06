"""
End-to-end route tests for the new bring-your-own-OAuth-app flow.
Real routes, real cookie handling via TestClient (which persists cookies
across requests like a browser); only the actual Google network calls
(token exchange, Drive API) are mocked.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, main, session, vault  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app=main.app, base_url="https://testserver")


def test_welcome_page_shows_dynamic_redirect_uris_and_no_hardcoded_credentials(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "/oauth/callback" in html
    assert "/oauth/callback/secondary" in html
    assert "Client ID" in html
    assert "Client Secret" in html
    # The core promise, stated explicitly to the user
    assert "never" in html.lower()
    assert "stores" in html.lower() or "stored" in html.lower()
    assert "encrypted" in html.lower()


def test_welcome_page_shows_error_banner_for_expired_setup(client):
    resp = client.get("/?error=setup_expired")
    assert "expired" in resp.text.lower()


def test_welcome_page_shows_error_banner_for_auth_failure(client):
    resp = client.get("/?error=auth_failed")
    assert "double check" in resp.text.lower() or "client secret" in resp.text.lower()


def test_index_redirects_to_dashboard_when_session_already_valid(client, monkeypatch):
    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: ({}, {"accounts": {}, "folders": [], "files": {}}))
    cookie = session.create_session_cookie("sub-1", "me@example.com", "rt", "cid", "csecret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/dashboard"


def test_start_setup_rejects_blank_client_id(client):
    resp = client.post("/start-setup", data={"email": "", "client_id": "   ", "client_secret": "x"})
    assert resp.status_code == 400


def test_start_setup_redirects_to_google_with_users_own_client_id_and_sets_pending_cookie(client):
    resp = client.post(
        "/start-setup",
        data={"email": "me@example.com", "client_id": "my-own-client-id.apps.googleusercontent.com", "client_secret": "my-secret"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "client_id=my-own-client-id.apps.googleusercontent.com" in location
    assert "prompt=consent" in location

    # Pending-setup cookie was set so the callback can retrieve it
    assert any(c.name == config.PENDING_SETUP_COOKIE_NAME for c in client.cookies.jar)


def test_oauth_callback_without_pending_cookie_redirects_with_error(client):
    from app import oauth_state
    state = oauth_state.make_state("login")
    resp = client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?error=setup_expired"


def test_oauth_callback_rejects_invalid_state(client):
    resp = client.get("/oauth/callback?code=abc&state=garbage")
    assert resp.status_code == 400


def test_oauth_callback_success_creates_session_and_writes_vault(client, monkeypatch):
    from app import oauth_state

    # Step 1: user submits their own credentials
    client.post(
        "/start-setup",
        data={"email": "me@example.com", "client_id": "user-client-id", "client_secret": "user-client-secret"},
        follow_redirects=False,
    )

    # Mock the actual Google network call
    monkeypatch.setattr(auth, "exchange_code_for_identity", lambda flow, code, client_id: {
        "sub": "google-sub-123", "email": "me@example.com", "refresh_token": "user-refresh-token",
    })

    saved_vaults = {}

    class FakeDriveClient:
        def __init__(self, refresh_token, scopes, client_id, client_secret, account_key=None):
            self.refresh_token = refresh_token
            self.client_id = client_id
            self.client_secret = client_secret

        def read_vault_raw(self):
            return None  # first-time user, no vault yet

        def write_vault_raw(self, content):
            saved_vaults["content"] = content

    monkeypatch.setattr(main, "DriveClient", FakeDriveClient)

    state = oauth_state.make_state("login")
    resp = client.get(f"/oauth/callback?code=abc123&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    # Session cookie now carries the USER's OWN credentials
    sess_cookie = next(c.value for c in client.cookies.jar if c.name == config.SESSION_COOKIE_NAME)
    sess = session.read_session_cookie(sess_cookie)
    assert sess["client_id"] == "user-client-id"
    assert sess["client_secret"] == "user-client-secret"
    assert sess["sub"] == "google-sub-123"

    # Pending-setup cookie has been cleared
    pending_cookies = [c for c in client.cookies.jar if c.name == config.PENDING_SETUP_COOKIE_NAME]
    assert pending_cookies == [] or pending_cookies[0].value in ("", None)

    # Vault (encrypted) was written containing the OAuth client backup
    assert "content" in saved_vaults
    from app.crypto import decrypt
    import json
    v = json.loads(decrypt(saved_vaults["content"]))
    client_id, client_secret = vault.get_oauth_client(v)
    assert client_id == "user-client-id"
    assert client_secret == "user-client-secret"
    # And the raw saved bytes never contain the plaintext secret
    assert "user-client-secret" not in saved_vaults["content"]


def test_oauth_callback_handles_exchange_failure_gracefully(client, monkeypatch):
    from app import oauth_state

    client.post(
        "/start-setup",
        data={"email": "", "client_id": "bad-client-id", "client_secret": "bad-secret"},
        follow_redirects=False,
    )

    def boom(flow, code, client_id):
        raise RuntimeError("invalid_client")

    monkeypatch.setattr(auth, "exchange_code_for_identity", boom)

    state = oauth_state.make_state("login")
    resp = client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?error=auth_failed"


def test_oauth_callback_success_also_sets_a_long_lived_remember_cookie(client, monkeypatch):
    from app import oauth_state

    client.post(
        "/start-setup",
        data={"email": "me@example.com", "client_id": "user-client-id", "client_secret": "user-client-secret"},
        follow_redirects=False,
    )
    monkeypatch.setattr(auth, "exchange_code_for_identity", lambda flow, code, client_id: {
        "sub": "google-sub-123", "email": "me@example.com", "refresh_token": "user-refresh-token",
    })

    class FakeDriveClient:
        def __init__(self, refresh_token, scopes, client_id, client_secret, account_key=None):
            pass

        def read_vault_raw(self):
            return None

        def write_vault_raw(self, content):
            pass

    monkeypatch.setattr(main, "DriveClient", FakeDriveClient)

    state = oauth_state.make_state("login")
    client.get(f"/oauth/callback?code=abc123&state={state}", follow_redirects=False)

    remember_cookie = next(c.value for c in client.cookies.jar if c.name == config.REMEMBER_COOKIE_NAME)
    remembered = session.read_remember_cookie(remember_cookie)
    assert remembered["email"] == "me@example.com"
    assert remembered["client_id"] == "user-client-id"
    assert remembered["client_secret"] == "user-client-secret"


def test_welcome_page_offers_one_click_continue_when_device_is_remembered(client):
    remember_cookie = session.create_remember_cookie("me@example.com", "remembered-client-id", "remembered-secret")
    client.cookies.set(config.REMEMBER_COOKIE_NAME, remember_cookie)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "me@example.com" in resp.text
    assert '/continue' in resp.text
    assert '/forget-device' in resp.text


def test_welcome_page_has_no_continue_prompt_without_a_remember_cookie(client):
    resp = client.get("/")
    assert 'href="/continue"' not in resp.text


def test_continue_redirects_to_google_using_remembered_credentials(client):
    remember_cookie = session.create_remember_cookie("me@example.com", "remembered-client-id", "remembered-secret")
    client.cookies.set(config.REMEMBER_COOKIE_NAME, remember_cookie)

    resp = client.get("/continue", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "client_id=remembered-client-id" in location
    assert any(c.name == config.PENDING_SETUP_COOKIE_NAME for c in client.cookies.jar)


def test_continue_without_remember_cookie_falls_back_to_home(client):
    resp = client.get("/continue", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/"


def test_forget_device_clears_remember_and_session_cookies(client):
    client.cookies.set(config.REMEMBER_COOKIE_NAME, session.create_remember_cookie("me@example.com", "cid", "csecret"))
    client.cookies.set(config.SESSION_COOKIE_NAME, session.create_session_cookie("sub-1", "me@example.com", "rt", "cid", "csecret"))

    resp = client.get("/forget-device", follow_redirects=False)
    assert resp.status_code in (302, 307)

    # Check the raw Set-Cookie response headers directly rather than the
    # client-side cookie jar -- delete_cookie()'s cleared cookie has no
    # explicit Domain, so TestClient's jar can end up tracking it as a
    # separate entry from the one seeded via cookies.set(), instead of
    # overwriting it.
    set_cookie_headers = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    assert any(h.startswith(f'{config.REMEMBER_COOKIE_NAME}=""') and "Max-Age=0" in h for h in set_cookie_headers)
    assert any(h.startswith(f'{config.SESSION_COOKIE_NAME}=""') and "Max-Age=0" in h for h in set_cookie_headers)


def test_connect_drive_reuses_session_client_id_without_asking_again(client):
    cookie = session.create_session_cookie("sub-1", "me@example.com", "rt", "already-configured-client-id", "already-configured-secret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)

    resp = client.get("/connect-drive", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "client_id=already-configured-client-id" in resp.headers["location"]
    assert "prompt=select_account" in resp.headers["location"] or "select_account" in resp.headers["location"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
