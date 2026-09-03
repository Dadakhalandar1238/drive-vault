"""
One DriveClient = one connected Google account. All Drive REST calls are
funneled through here so the rest of the app never touches the Google
API directly.
"""
from __future__ import annotations

import io

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from . import config, token_cache

UNLIMITED_QUOTA_FALLBACK = 10**15  # Workspace accounts often report no "limit"
VAULT_FILENAME = "vault.enc"


class DriveClient:
    def __init__(self, refresh_token: str, scopes: list[str], client_id: str, client_secret: str, account_key: str | None = None):
        self.refresh_token = refresh_token
        self.scopes = scopes
        self.client_id = client_id
        self.client_secret = client_secret
        # account_key (the Google account's own "sub") is how this client
        # finds and updates its cached access token -- see token_cache.py.
        # It's optional so ad-hoc/test clients still work without one.
        self.account_key = account_key

        cached = token_cache.get(account_key)
        access_token, expiry = cached if cached else (None, None)
        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            expiry=expiry,
        )
        self._creds = creds
        # google-api-python-client's httplib2 transport is explicitly NOT
        # thread-safe -- concurrent requests sharing one connection can
        # corrupt each other and hang until the socket times out. We give
        # every DriveClient its own Http object (with a real timeout so a
        # genuine network stall fails fast instead of hanging forever),
        # and fresh_copy() below lets concurrent callers get their own
        # instance instead of sharing this one across threads.
        http = httplib2.Http(timeout=config.DRIVE_REQUEST_TIMEOUT)
        authorized_http = AuthorizedHttp(creds, http=http)
        # cache_discovery=False avoids writing to disk, keeping the
        # container filesystem untouched (important for stateless hosts)
        self._service = build("drive", "v3", http=authorized_http, cache_discovery=False)

    def fresh_copy(self) -> "DriveClient":
        """A new, independent client for the same account -- safe to hand
        to a different thread running concurrently with this one. Picks up
        whatever's currently cached for this account, so if a sibling
        already refreshed the token, this copy reuses it instead of
        refreshing again."""
        return DriveClient(self.refresh_token, self.scopes, self.client_id, self.client_secret, account_key=self.account_key)

    def _touch_cache(self) -> None:
        """Called after every real Drive API call so the next DriveClient
        built for this account (this request or a later one) can skip
        re-authenticating if the token is still valid."""
        token_cache.set(self.account_key, self._creds.token, self._creds.expiry)

    # ---- capacity -------------------------------------------------
    def get_storage_info(self) -> dict:
        """Returns {'limit': int|None, 'usage': int, 'free': int}. limit is
        None for accounts with unlimited storage (e.g. some Workspace plans)."""
        about = self._service.about().get(fields="storageQuota").execute()
        self._touch_cache()
        quota = about.get("storageQuota", {})
        limit = quota.get("limit")
        usage = int(quota.get("usage", 0))
        if limit is None:
            return {"limit": None, "usage": usage, "free": UNLIMITED_QUOTA_FALLBACK}
        limit = int(limit)
        return {"limit": limit, "usage": usage, "free": max(limit - usage, 0)}

    def get_free_space(self) -> int:
        return self.get_storage_info()["free"]

    # ---- generic file storage (used for user's managed files) -----
    def upload_bytes(self, name: str, data: bytes, mime_type: str = "application/octet-stream") -> str:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        file = self._service.files().create(
            body={"name": name}, media_body=media, fields="id"
        ).execute()
        self._touch_cache()
        return file["id"]

    def download_bytes(self, file_id: str) -> bytes:
        request = self._service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        self._touch_cache()
        return buf.getvalue()

    def delete_file(self, file_id: str) -> None:
        self._service.files().delete(fileId=file_id).execute()
        self._touch_cache()

    # ---- hidden appDataFolder, used only by the primary account ----
    def _find_appdata_file_id(self) -> str | None:
        resp = self._service.files().list(
            spaces="appDataFolder",
            q=f"name='{VAULT_FILENAME}'",
            fields="files(id)",
        ).execute()
        self._touch_cache()
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def read_vault_raw(self) -> str | None:
        file_id = self._find_appdata_file_id()
        if not file_id:
            return None
        return self.download_bytes(file_id).decode()

    def write_vault_raw(self, content: str) -> None:
        file_id = self._find_appdata_file_id()
        media = MediaIoBaseUpload(io.BytesIO(content.encode()), mimetype="application/json")
        if file_id:
            self._service.files().update(fileId=file_id, media_body=media).execute()
        else:
            self._service.files().create(
                body={"name": VAULT_FILENAME, "parents": ["appDataFolder"]},
                media_body=media,
                fields="id",
            ).execute()
        self._touch_cache()

    def write_backup_blob(self, name: str, content: str) -> str:
        """Writes a standalone, never-overwritten file into appDataFolder.
        Used to preserve an undecryptable vault before replacing it, so a
        SECRET_KEY mismatch doesn't silently destroy data with no way back."""
        media = MediaIoBaseUpload(io.BytesIO(content.encode()), mimetype="application/octet-stream")
        file = self._service.files().create(
            body={"name": name, "parents": ["appDataFolder"]},
            media_body=media,
            fields="id",
        ).execute()
        self._touch_cache()
        return file["id"]
