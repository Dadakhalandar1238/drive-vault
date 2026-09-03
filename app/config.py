"""
All configuration comes from environment variables so the app stays
stateless and works identically on localhost, Render, Cloud Run, etc.
"""
from __future__ import annotations

import os

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

# Must exactly match an "Authorized redirect URI" configured in the
# Google Cloud OAuth client, e.g. https://your-app.onrender.com/oauth/callback
REDIRECT_URI = os.environ["REDIRECT_URI"]
SECONDARY_REDIRECT_URI = os.environ.get(
    "SECONDARY_REDIRECT_URI", REDIRECT_URI.replace("/oauth/callback", "/oauth/callback/secondary")
)

# Random 32+ byte secret used to sign session cookies and encrypt
# refresh tokens. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
SECRET_KEY = os.environ["SECRET_KEY"]

# Non-sensitive scope set on purpose -- avoids Google's "restricted scope"
# security-assessment requirement that full drive access would trigger.
# drive.file  -> app can only see/manage files IT creates
# drive.appdata -> hidden per-account app storage, used for the vault
# openid/email -> just to label which Google account is which
SCOPES_PRIMARY = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.appdata",
]
SCOPES_SECONDARY = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]

# Optional: only needed for the "Import from Drive" (Google Picker)
# feature. Without it, that button is simply hidden -- everything else
# works exactly as before. Get one from Google Cloud Console -> APIs &
# Services -> Credentials -> Create Credentials -> API key, then restrict
# it to the Google Picker API and your deployed domain.
GOOGLE_PICKER_API_KEY = os.environ.get("GOOGLE_PICKER_API_KEY", "")

MAX_DRIVES_PER_USER = int(os.environ.get("MAX_DRIVES_PER_USER", 10))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 10))
# Generous timeout since a chunk upload/download can legitimately take a
# while on a slow connection -- this exists to fail clearly instead of
# hanging forever if something actually goes wrong on the wire.
DRIVE_REQUEST_TIMEOUT = int(os.environ.get("DRIVE_REQUEST_TIMEOUT", 300))
SESSION_COOKIE_NAME = "dv_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
