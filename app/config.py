"""
All configuration comes from environment variables so the app stays
stateless and works identically on localhost, Render, Cloud Run, etc.

Notably absent: any Google OAuth Client ID/Secret. This app does not have
one of its own -- every user brings their own Google Cloud project when
they sign in (see the welcome page / onboarding flow), and it's never
persisted here. This file only holds things that are genuinely safe to
share across every visitor to this deployment.
"""
from __future__ import annotations

import os

# Random 32+ byte secret used to sign/encrypt session cookies. This is
# the only "credential" this deployment itself holds, and it never
# grants access to anyone's Drive by itself. Generate with:
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
# feature. This one IS shared across visitors deliberately -- unlike the
# OAuth Client ID/Secret, a Picker API key doesn't grant access to
# anyone's files by itself, it just identifies API traffic for quota
# purposes. Without it, the Import button is simply hidden.
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
PENDING_SETUP_COOKIE_NAME = "dv_pending_setup"

# Separate from the session cookie so a returning user can skip retyping
# their Client ID/Secret even after the session above has expired or been
# cleared -- see session.py's remember-cookie functions for what it holds.
REMEMBER_COOKIE_NAME = "dv_remember"
REMEMBER_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
