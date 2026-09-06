"""
End-to-end route tests for the shared-OAuth-app sign-in flow. Real
routes, real cookie handling via TestClient (which persists cookies
across requests like a browser); only the actual Google network calls
(token exchange, Drive API) are mocked.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "shared-app-client-id.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "shared-app-client-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, main, session  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app=main.app, base_url="https://testserver")


def test_welcome_page_shows_sign_in_button(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/login"' in html
    assert "Sign in with Google" in html
    # No per-user credential fields anywhere on the page
    assert "Client ID" not in html
    assert "Client Secret" not in html


def test_welcome_page_shows_error_banner_for_auth_failure(client):
    resp = client.get("/?error=auth_failed")
    assert "complete sign-in" in resp.text.lower()


def test_index_redirects_to_dashboard_when_session_already_valid(client, monkeypatch):
    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: ({}, {"accounts": {}, "folders": [], "files": {}}))
    cookie = session.create_session_cookie("sub-1", "me@example.com", "rt")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/dashboard"


def test_login_redirects_to_google_using_the_shared_app_credentials(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    # config.GOOGLE_CLIENT_ID rather than a hardcoded literal -- this env
    # var is process-global, so whichever test module's os.environ import
    # ran first "wins" the actual value under pytest's single process.
    assert f"client_id={config.GOOGLE_CLIENT_ID}" in location
    assert "prompt=consent" in location


def test_oauth_callback_rejects_invalid_state(client):
    resp = client.get("/oauth/callback?code=abc&state=garbage")
    assert resp.status_code == 400


def test_oauth_callback_handles_exchange_failure_gracefully(client, monkeypatch):
    from app import oauth_state

    def boom(flow, code, client_id):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(auth, "exchange_code_for_identity", boom)

    state = oauth_state.make_state("login")
    resp = client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?error=auth_failed"


def test_oauth_callback_success_creates_session_and_writes_vault(client, monkeypatch):
    from app import oauth_state

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

    sess_cookie = next(c.value for c in client.cookies.jar if c.name == config.SESSION_COOKIE_NAME)
    sess = session.read_session_cookie(sess_cookie)
    assert sess["sub"] == "google-sub-123"
    assert sess["email"] == "me@example.com"
    assert sess["refresh_token"] == "user-refresh-token"

    assert "content" in saved_vaults


def test_connect_drive_uses_the_shared_app_credentials(client):
    cookie = session.create_session_cookie("sub-1", "me@example.com", "rt")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)

    resp = client.get("/connect-drive", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert f"client_id={config.GOOGLE_CLIENT_ID}" in location
    assert "select_account" in location


def test_logout_clears_session_cookie(client):
    cookie = session.create_session_cookie("sub-1", "me@example.com", "rt")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)

    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code in (302, 307)
    set_cookie_headers = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    assert any(h.startswith(f'{config.SESSION_COOKIE_NAME}=""') for h in set_cookie_headers)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
