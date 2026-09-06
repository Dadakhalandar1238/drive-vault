"""
Builds and drives the Google OAuth2 "Authorization Code" flow. Used both
for the primary sign-in (establishes the user's identity + session) and
for adding secondary drives (just adds another refresh token to the vault).

Every function here takes client_id/client_secret as parameters rather
than importing config directly, so this module stays easy to test in
isolation -- but in practice every caller in main.py passes the one
shared app's config.GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET.
"""
from __future__ import annotations

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def build_flow(client_id: str, client_secret: str, scopes: list[str], redirect_uri: str, state: str | None = None) -> Flow:
    return Flow.from_client_config(
        _client_config(client_id, client_secret), scopes=scopes, redirect_uri=redirect_uri, state=state
    )


def get_authorization_url(flow: Flow, force_account_chooser: bool = False) -> str:
    prompt = "select_account consent" if force_account_chooser else "consent"
    url, _ = flow.authorization_url(
        access_type="offline",       # required to receive a refresh_token
        include_granted_scopes="true",
        prompt=prompt,
    )
    return url


def exchange_code_for_identity(flow: Flow, code: str, client_id: str) -> dict:
    """Exchanges an auth code for tokens and returns {sub, email, refresh_token}."""
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token. This usually means the "
            "account already granted consent previously without 'prompt=consent'."
        )
    info = google_id_token.verify_oauth2_token(
        creds.id_token, GoogleAuthRequest(), audience=client_id
    )
    return {
        "sub": info["sub"],
        "email": info.get("email", "unknown"),
        "refresh_token": creds.refresh_token,
    }
