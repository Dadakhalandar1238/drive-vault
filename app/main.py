from __future__ import annotations

import json
import logging
import os

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import auth, config, distributor, oauth_state, session, upload_sessions, vault
from .drive_client import DriveClient, TRANSFER_CHUNK_SIZE

logger = logging.getLogger(__name__)

app = FastAPI(title="Drive Vault")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

GB = 1024 ** 3


def _gauge_level(percent_used: float) -> str:
    """Which color band a gauge should render in."""
    if percent_used >= 90:
        return "critical"
    if percent_used >= 70:
        return "warning"
    return "healthy"


# ---------------------------------------------------------------------
# session / client helpers
# ---------------------------------------------------------------------
def get_current_session(request: Request) -> dict | None:
    cookie = request.cookies.get(config.SESSION_COOKIE_NAME)
    return session.read_session_cookie(cookie)


def require_session(request: Request) -> dict:
    sess = get_current_session(request)
    if sess is None:
        raise HTTPException(status_code=303, headers={"Location": "/"}, detail="Not logged in")
    return sess


def build_clients_and_vault(sess: dict) -> tuple[dict[str, DriveClient], dict]:
    """
    Returns (clients, vault) where clients is every connected drive for
    this user (primary + secondaries), keyed by google account id. All of
    them share this one user's own Client ID/Secret -- one Google Cloud
    OAuth app can authorize as many different Google accounts as it likes,
    so there's no need for a separate app per connected drive.
    """
    primary_client = DriveClient(
        sess["refresh_token"], config.SCOPES_PRIMARY,
        sess["client_id"], sess["client_secret"], account_key=sess["sub"],
    )
    v = vault.load_vault(primary_client)
    vault.register_primary_account(v, sess["sub"], sess["email"])

    clients = {sess["sub"]: primary_client}
    for acct_id, acct in v["accounts"].items():
        if acct.get("primary"):
            continue
        clients[acct_id] = DriveClient(
            vault.decrypted_refresh_token(v, acct_id), config.SCOPES_SECONDARY,
            sess["client_id"], sess["client_secret"], account_key=acct_id,
        )
    return clients, v


# ---------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------
_ERROR_MESSAGES = {
    "setup_expired": "Your setup session expired after 15 minutes of inactivity -- please fill in your Client ID and Client Secret again below.",
    "auth_failed": "Google couldn't complete sign-in with those credentials -- double check your Client ID and Client Secret (typos are the most common cause) and try again.",
}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str = ""):
    if get_current_session(request):
        return RedirectResponse("/dashboard")

    remembered = session.read_remember_cookie(request.cookies.get(config.REMEMBER_COOKIE_NAME))
    return templates.TemplateResponse(request, "welcome.html", {
        "redirect_uri_primary": str(request.url_for("oauth_callback")),
        "redirect_uri_secondary": str(request.url_for("oauth_callback_secondary")),
        "error_message": _ERROR_MESSAGES.get(error, ""),
        "remembered_email": remembered["email"] if remembered else "",
    })


def _compute_storage_view(v: dict, overview: dict) -> dict:
    """Turns raw per-account quota info into the gauge-ready shape used by
    both the dashboard's async refresh and could be reused elsewhere."""
    accounts = {}
    for acct_id, acct in v["accounts"].items():
        info = overview.get(acct_id, {"limit": None, "usage": 0, "free": 0})
        unlimited = info["limit"] is None
        percent_used = 0 if unlimited else round(info["usage"] / info["limit"] * 100, 1) if info["limit"] else 0
        accounts[acct_id] = {
            "unlimited": unlimited,
            "usage_gb": round(info["usage"] / GB, 2),
            "free_gb": None if unlimited else round(info["free"] / GB, 2),
            "limit_gb": None if unlimited else round(info["limit"] / GB, 1),
            "percent_used": percent_used,
            "level": _gauge_level(percent_used),
        }

    known = [info for info in overview.values() if info["limit"] is not None]
    unlimited_count = sum(1 for info in overview.values() if info["limit"] is None)
    total_capacity = sum(info["limit"] for info in known)
    total_usage = sum(info["usage"] for info in overview.values())
    total_free = sum(info["free"] for info in known)
    pool_percent = round(total_usage / total_capacity * 100, 1) if total_capacity > 0 else None

    pool = {
        "total_capacity_gb": round(total_capacity / GB, 1) if total_capacity else None,
        "total_usage_gb": round(total_usage / GB, 2),
        "total_free_gb": round(total_free / GB, 1) if total_capacity else None,
        "percent_used": pool_percent,
        "level": _gauge_level(pool_percent) if pool_percent is not None else "healthy",
        "unlimited_count": unlimited_count,
    }
    return {"accounts": accounts, "pool": pool}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, folder: str = ""):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)
    current_folder = vault.normalize_path(folder)

    # Live free-space numbers are NOT fetched here on purpose -- that would
    # mean every folder click waits on a quota check against every
    # connected drive. The page renders immediately from vault data alone
    # (label/email only, no numbers yet); the browser fetches the live
    # numbers right after via /api/storage-overview and fills them in.
    accounts = []
    for acct_id, acct in v["accounts"].items():
        accounts.append({
            "key": acct_id,
            "label": acct["label"],
            "email": acct["email"],
            "is_primary": acct.get("primary", False),
        })

    subfolder_paths, file_keys = vault.list_folder_contents(v, current_folder)
    subfolders = [{"path": p, "name": p.split("/")[-1]} for p in subfolder_paths]

    files = []
    for key in file_keys:
        info = v["files"][key]
        files.append({
            "key": key,
            "name": key.split("/")[-1],
            "size_mb": round(info["size"] / (1024 ** 2), 3),
            "uploaded_at": info["uploaded_at"],
            "num_chunks": len(info["chunks"]),
        })

    # Breadcrumbs: [("Home", ""), ("docs", "docs"), ("receipts", "docs/receipts")]
    breadcrumbs = [{"name": "Home", "path": ""}]
    if current_folder:
        parts = current_folder.split("/")
        for i, part in enumerate(parts):
            breadcrumbs.append({"name": part, "path": "/".join(parts[:i + 1])})

    return templates.TemplateResponse(request, "dashboard.html", {
        "email": sess["email"],
        "accounts": accounts,
        "current_folder": current_folder,
        "breadcrumbs": breadcrumbs,
        "subfolders": subfolders,
        "files": files,
        "num_drives": len(v["accounts"]),
        "max_drives": config.MAX_DRIVES_PER_USER,
        "can_add_drive": len(v["accounts"]) < config.MAX_DRIVES_PER_USER,
        "google_client_id": sess["client_id"],
        "google_picker_api_key": config.GOOGLE_PICKER_API_KEY,
    })


@app.get("/api/storage-overview")
def api_storage_overview(request: Request):
    """Fetched by the dashboard's JS right after page load, so folder
    navigation itself never has to wait on live quota checks across every
    connected drive."""
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)
    overview = distributor.get_storage_overview(clients)
    return _compute_storage_view(v, overview)


# ---------------------------------------------------------------------
# auth: first-time setup + primary login
# ---------------------------------------------------------------------
def _redirect_to_google_login(request: Request, email: str, client_id: str, client_secret: str) -> RedirectResponse:
    """Shared by /start-setup (freshly-typed credentials) and /continue
    (credentials pulled from the remember cookie) -- both just need to kick
    off the same OAuth round trip."""
    redirect_uri = str(request.url_for("oauth_callback"))
    flow = auth.build_flow(client_id, client_secret, config.SCOPES_PRIMARY, redirect_uri, state=oauth_state.make_state("login"))
    url = auth.get_authorization_url(flow, force_account_chooser=False)

    resp = RedirectResponse(url, status_code=303)
    # Holds the credentials JUST long enough to survive the round trip to
    # Google and back -- never written to disk, and cleared the moment
    # /oauth/callback finishes with it.
    cookie_value = session.create_pending_setup_cookie(email, client_id, client_secret)
    resp.set_cookie(
        config.PENDING_SETUP_COOKIE_NAME, cookie_value,
        max_age=session.PENDING_SETUP_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    return resp


@app.post("/start-setup")
def start_setup(request: Request, email: str = Form(""), client_id: str = Form(...), client_secret: str = Form(...)):
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "Client ID and Client Secret are both required.")
    return _redirect_to_google_login(request, email.strip(), client_id, client_secret)


@app.get("/continue")
def continue_with_remembered(request: Request):
    """Re-login for a returning user whose session has expired or been
    cleared. If the remembered refresh token still works, this resumes
    the existing Google grant silently -- no redirect to Google, no
    consent screen, since nothing new is being authorized. Only falls
    back to a full Google round trip (still using the same remembered
    Client ID/Secret, so no retyping) if that token no longer works."""
    remembered = session.read_remember_cookie(request.cookies.get(config.REMEMBER_COOKIE_NAME))
    if remembered is None:
        return RedirectResponse("/")

    if remembered["refresh_token"] and remembered["sub"]:
        try:
            primary_client = DriveClient(
                remembered["refresh_token"], config.SCOPES_PRIMARY,
                remembered["client_id"], remembered["client_secret"], account_key=remembered["sub"],
            )
            primary_client.get_storage_info()  # cheap call; forces a real token refresh, proving it's still valid
        except Exception:
            logger.info("Remembered refresh token no longer valid for client_id=%s -- falling back to full Google sign-in.", remembered["client_id"])
        else:
            cookie_value = session.create_session_cookie(
                remembered["sub"], remembered["email"], remembered["refresh_token"],
                remembered["client_id"], remembered["client_secret"],
            )
            resp = RedirectResponse("/dashboard", status_code=303)
            resp.set_cookie(
                config.SESSION_COOKIE_NAME, cookie_value,
                max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
            )
            # Slides the remember cookie's own 1-year expiry forward too,
            # so an actively-used "remembered" browser never quietly ages out.
            remember_cookie_value = session.create_remember_cookie(
                remembered["sub"], remembered["email"], remembered["client_id"], remembered["client_secret"], remembered["refresh_token"],
            )
            resp.set_cookie(
                config.REMEMBER_COOKIE_NAME, remember_cookie_value,
                max_age=config.REMEMBER_MAX_AGE, httponly=True, samesite="lax", secure=True,
            )
            return resp

    return _redirect_to_google_login(request, remembered["email"], remembered["client_id"], remembered["client_secret"])


@app.get("/forget-device")
def forget_device():
    """Clears the remember cookie (and any session) so the next visit shows
    the full manual credential form again -- for a shared/borrowed browser,
    or switching to a different Google Cloud project."""
    resp = RedirectResponse("/")
    resp.delete_cookie(config.REMEMBER_COOKIE_NAME)
    resp.delete_cookie(config.SESSION_COOKIE_NAME)
    return resp


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str, state: str):
    if not oauth_state.verify_state(state, "login"):
        raise HTTPException(400, "Invalid or expired OAuth state")

    pending = session.read_pending_setup_cookie(request.cookies.get(config.PENDING_SETUP_COOKIE_NAME))
    if pending is None:
        return RedirectResponse("/?error=setup_expired", status_code=303)

    redirect_uri = str(request.url_for("oauth_callback"))
    flow = auth.build_flow(pending["client_id"], pending["client_secret"], config.SCOPES_PRIMARY, redirect_uri, state=state)
    try:
        identity = auth.exchange_code_for_identity(flow, code, pending["client_id"])
    except Exception:
        logger.warning("OAuth token exchange failed for client_id=%s, redirect_uri=%s", pending["client_id"], redirect_uri, exc_info=True)
        return RedirectResponse("/?error=auth_failed", status_code=303)

    # Bootstrap the vault immediately so the encrypted backup of this
    # user's own Client ID/Secret lives in their Drive from first sign-in.
    primary_client = DriveClient(
        identity["refresh_token"], config.SCOPES_PRIMARY,
        pending["client_id"], pending["client_secret"], account_key=identity["sub"],
    )
    v = vault.load_vault(primary_client)
    vault.register_primary_account(v, identity["sub"], identity["email"])
    vault.set_oauth_client(v, pending["client_id"], pending["client_secret"])
    vault.save_vault(primary_client, v)

    cookie_value = session.create_session_cookie(
        identity["sub"], identity["email"], identity["refresh_token"],
        pending["client_id"], pending["client_secret"],
    )
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        config.SESSION_COOKIE_NAME, cookie_value,
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    # Refreshed on every successful login (sliding 1-year expiry) so a
    # returning user keeps getting the one-click /continue path instead of
    # the credential form, for as long as they keep coming back. Carries
    # the refresh token too, so /continue can resume silently -- see
    # session.py's create_remember_cookie docstring.
    remember_cookie_value = session.create_remember_cookie(
        identity["sub"], identity["email"], pending["client_id"], pending["client_secret"], identity["refresh_token"],
    )
    resp.set_cookie(
        config.REMEMBER_COOKIE_NAME, remember_cookie_value,
        max_age=config.REMEMBER_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    resp.delete_cookie(config.PENDING_SETUP_COOKIE_NAME)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(config.SESSION_COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------
# auth: add a secondary drive (reuses the SAME Client ID/Secret already
# on file for this session -- one Google Cloud app, many connected drives)
# ---------------------------------------------------------------------
@app.get("/connect-drive")
def connect_drive(request: Request):
    sess = require_session(request)
    redirect_uri = str(request.url_for("oauth_callback_secondary"))
    flow = auth.build_flow(sess["client_id"], sess["client_secret"], config.SCOPES_SECONDARY, redirect_uri, state=oauth_state.make_state("secondary"))
    url = auth.get_authorization_url(flow, force_account_chooser=True)
    return RedirectResponse(url)


@app.get("/oauth/callback/secondary")
def oauth_callback_secondary(request: Request, code: str, state: str):
    sess = require_session(request)
    if not oauth_state.verify_state(state, "secondary"):
        raise HTTPException(400, "Invalid or expired OAuth state")

    redirect_uri = str(request.url_for("oauth_callback_secondary"))
    flow = auth.build_flow(sess["client_id"], sess["client_secret"], config.SCOPES_SECONDARY, redirect_uri, state=state)
    try:
        identity = auth.exchange_code_for_identity(flow, code, sess["client_id"])
    except Exception:
        logger.warning("OAuth token exchange failed (secondary drive) for client_id=%s, redirect_uri=%s", sess["client_id"], redirect_uri, exc_info=True)
        raise HTTPException(400, "Couldn't complete sign-in for that account -- please try again.")

    if identity["sub"] == sess["sub"]:
        raise HTTPException(400, "That's already your primary account -- pick a different Google account.")

    primary_client = DriveClient(
        sess["refresh_token"], config.SCOPES_PRIMARY,
        sess["client_id"], sess["client_secret"], account_key=sess["sub"],
    )
    v = vault.load_vault(primary_client)
    vault.register_primary_account(v, sess["sub"], sess["email"])

    if len(v["accounts"]) >= config.MAX_DRIVES_PER_USER:
        raise HTTPException(400, f"Drive limit reached ({config.MAX_DRIVES_PER_USER}).")
    if identity["sub"] in v["accounts"]:
        return RedirectResponse("/dashboard")  # already connected

    label = f"Drive {len(v['accounts']) + 1}"
    vault.add_or_update_account(v, identity["sub"], identity["email"], identity["refresh_token"], label)
    vault.save_vault(primary_client, v)
    return RedirectResponse("/dashboard")


# ---------------------------------------------------------------------
# file operations
# ---------------------------------------------------------------------
@app.get("/upload/config")
def upload_config():
    """The one source of truth for chunk size lives server-side
    (drive_client.TRANSFER_CHUNK_SIZE) -- the dashboard's JS fetches it
    rather than hardcoding a second copy that could drift out of sync."""
    return {"chunk_size": TRANSFER_CHUNK_SIZE}


@app.post("/upload/init")
def upload_init(request: Request, payload: dict = Body(...)):
    """Starts a new resumable-upload session, or -- if one already
    exists for this exact (session_id, filename, size) -- reports how
    far it already got, so the browser knows whether to resume mid-file
    instead of restarting. session_id is computed client-side from the
    file's own name/size/lastModified, so re-selecting the same file
    later (even after closing the tab) reaches the same session."""
    sess = require_session(request)
    folder = vault.normalize_path(payload.get("folder", ""))
    try:
        status = upload_sessions.init_session(
            sess["sub"], payload["session_id"], payload["filename"], folder,
            total_size=int(payload["total_size"]), chunk_size=int(payload["chunk_size"]),
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return status


@app.post("/upload/chunk")
async def upload_chunk(request: Request, session_id: str = Form(...), chunk_index: int = Form(...), chunk: UploadFile = File(...)):
    sess = require_session(request)
    data = await chunk.read()  # bounded by the client's own chunk size (matches TRANSFER_CHUNK_SIZE) -- fine to hold briefly
    try:
        received_chunks = upload_sessions.write_chunk(sess["sub"], session_id, chunk_index, data)
    except upload_sessions.SessionError as e:
        # 409: the client's view of this session has drifted (e.g. two
        # tabs uploading the same file at once) -- it should re-sync via
        # /upload/init rather than treat this as a fatal failure.
        raise HTTPException(409, str(e))
    return {"received_chunks": received_chunks}


@app.post("/upload/complete")
def upload_complete(request: Request, payload: dict = Body(...)):
    """Called once the browser has sent every chunk. Hands the now
    fully-assembled file off to the exact same distributor pipeline a
    direct upload always used -- chunked-to-the-server and
    chunked-across-drives are two independent, unrelated splits."""
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)
    session_id = payload["session_id"]

    try:
        fd, size, filename, folder = upload_sessions.finalize_session(sess["sub"], session_id)
    except upload_sessions.SessionError as e:
        raise HTTPException(409, str(e))

    key = f"{folder}/{filename}" if folder else filename
    try:
        grouped_chunks = distributor.upload_many(clients, [(key, fd, size)])
    except ValueError as e:
        # Not enough space across connected drives right now -- the
        # assembled upload is left in place so retrying just this step
        # (after freeing space, or connecting another drive) doesn't
        # require resending the file's bytes.
        os.close(fd)
        raise HTTPException(400, str(e))

    os.close(fd)
    # Distribution to Drive succeeded -- clean up now rather than after
    # the vault save below, so a later retry (if that save somehow
    # fails) can't re-upload the same file's bytes a second time.
    upload_sessions.cleanup_session(sess["sub"], session_id)

    vault.record_file(v, key, size, grouped_chunks[key])
    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return {"status": "ok", "key": key, "folder": folder}


@app.get("/download/{filename:path}")
def download(request: Request, filename: str):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    info = v["files"].get(filename)
    if not info:
        raise HTTPException(404, "File not found")

    display_name = filename.split("/")[-1]
    # Every chunk is fetched concurrently straight to its correct offset
    # in this temp file (see distributor.download_with_chunks) -- never
    # assembled in memory. FileResponse then streams it off disk in
    # bounded pieces; the background task cleans it up once that's done.
    tmp_path = distributor.download_with_chunks(clients, info["chunks"])
    return FileResponse(
        tmp_path,
        media_type="application/octet-stream",
        filename=display_name,
        background=BackgroundTask(os.remove, tmp_path),
    )


@app.delete("/files/{filename:path}")
def delete_file(request: Request, filename: str):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    info = vault.remove_file(v, filename)
    if not info:
        raise HTTPException(404, "File not found")

    distributor.delete_chunks(clients, info["chunks"])
    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return {"status": "deleted"}


@app.post("/files/batch-delete")
def batch_delete_files(request: Request, payload: dict = Body(...)):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    filenames = payload.get("filenames", [])
    all_chunks = []
    removed = []
    for name in filenames:
        info = vault.remove_file(v, name)
        if info:
            all_chunks.extend(info["chunks"])
            removed.append(name)

    if all_chunks:
        # One combined parallel pass across every chunk of every selected
        # file -- same fan-out the multi-file upload path uses.
        distributor.delete_chunks(clients, all_chunks)

    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return {"status": "deleted", "removed": removed}


# ---------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------
@app.post("/folders/create")
def create_folder(request: Request, parent: str = Form(""), name: str = Form(...)):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    if not vault.is_valid_folder_name(name):
        raise HTTPException(400, "Invalid folder name")

    full_path = vault.create_folder(v, parent, name)
    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return RedirectResponse(f"/dashboard?folder={full_path}", status_code=303)


@app.post("/folders/delete")
def delete_folder(request: Request, path: str = Body(..., embed=True)):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    path = vault.normalize_path(path)
    chunks = vault.delete_folder_recursive(v, path)
    if chunks:
        distributor.delete_chunks(clients, chunks)

    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return {"status": "deleted", "path": path}
