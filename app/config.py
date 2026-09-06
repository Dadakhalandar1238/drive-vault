"""
All configuration comes from environment variables so the app stays
stateless and works identically on localhost, Render, Cloud Run, etc.

One shared Google OAuth app serves every user of this deployment -- the
deployer creates it once (see the README's setup guide) and configures it
here. Users themselves never see, enter, or need to safeguard a Client
ID/Secret; they just click "Sign in with Google". This deliberately trades
away per-user API quota isolation for the simple fact that nobody can lose
a credential they never had to hold in the first place.
"""
from __future__ import annotations

import os

# Random 32+ byte secret used to sign/encrypt session cookies. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
SECRET_KEY = os.environ["SECRET_KEY"]

# The one Google Cloud OAuth Client ID/Secret this whole deployment uses.
# Created once by whoever deploys this app (see README) -- never entered
# or stored by individual users.
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

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
# feature. A Picker API key doesn't grant access to anyone's files by
# itself, it just identifies API traffic for quota purposes. Without it,
# the Import button is simply hidden.
GOOGLE_PICKER_API_KEY = os.environ.get("GOOGLE_PICKER_API_KEY", "")

MAX_DRIVES_PER_USER = int(os.environ.get("MAX_DRIVES_PER_USER", 10))
# Deliberately conservative default: this app targets free-tier hosting
# (e.g. Render's 512MB RAM limit), and each concurrent upload/download
# can hold several times its own file size in memory at once (read into
# memory, sliced for chunking, then buffered again for the HTTP request).
# 10 concurrent big-file transfers on 512MB is a real way to get
# OOM-killed. Raise this via env var if you deploy somewhere with more
# RAM and want faster multi-file uploads.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 4))
# Generous timeout since a chunk upload/download can legitimately take a
# while on a slow connection -- this exists to fail clearly instead of
# hanging forever if something actually goes wrong on the wire.
DRIVE_REQUEST_TIMEOUT = int(os.environ.get("DRIVE_REQUEST_TIMEOUT", 300))
SESSION_COOKIE_NAME = "dv_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
