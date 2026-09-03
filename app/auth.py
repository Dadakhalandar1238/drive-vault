"""
Builds and drives the Google OAuth2 "Authorization Code" flow. Used both
for the primary sign-in (establishes the user's identity + session) and
for adding secondary drives (just adds another refresh token to the vault).
"""
from __future__ import annotations

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow

from . import config


def _client_config() -> dict:
    return {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def build_flow(scopes: list[str], redirect_uri: str, state: str | None = None) -> Flow:
    return Flow.from_client_config(
        _client_config(), scopes=scopes, redirect_uri=redirect_uri, state=state
    )


def get_authorization_url(flow: Flow, force_account_chooser: bool = False) -> str:
    prompt = "select_account consent" if force_account_chooser else "consent"
    url, _ = flow.authorization_url(
        access_type="offline",       # required to receive a refresh_token
        include_granted_scopes="true",
        prompt=prompt,
    )
    return url


def exchange_code_for_identity(flow: Flow, code: str) -> dict:
    """Exchanges an auth code for tokens and returns {sub, email, refresh_token}."""
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token. This usually means the "
            "account already granted consent previously without 'prompt=consent'."
        )
    info = google_id_token.verify_oauth2_token(
        creds.id_token, GoogleAuthRequest(), audience=config.GOOGLE_CLIENT_ID
    )
    return {
        "sub": info["sub"],
        "email": info.get("email", "unknown"),
        "refresh_token": creds.refresh_token,
    }
