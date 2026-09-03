from __future__ import annotations

import io
import json

from fastapi import Body, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, distributor, oauth_state, session, vault
from .drive_client import DriveClient

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
    this user (primary + secondaries), keyed by google account id.
    """
    primary_client = DriveClient(sess["refresh_token"], config.SCOPES_PRIMARY, account_key=sess["sub"])
    v = vault.load_vault(primary_client)
    vault.register_primary_account(v, sess["sub"], sess["email"])

    clients = {sess["sub"]: primary_client}
    for acct_id, acct in v["accounts"].items():
        if acct.get("primary"):
            continue
        clients[acct_id] = DriveClient(vault.decrypted_refresh_token(v, acct_id), config.SCOPES_SECONDARY, account_key=acct_id)
    return clients, v


# ---------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if get_current_session(request):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "login.html", {})


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
        "google_client_id": config.GOOGLE_CLIENT_ID,
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
# auth: primary login
# ---------------------------------------------------------------------
@app.get("/login")
def login():
    flow = auth.build_flow(config.SCOPES_PRIMARY, config.REDIRECT_URI, state=oauth_state.make_state("login"))
    url = auth.get_authorization_url(flow, force_account_chooser=False)
    return RedirectResponse(url)


@app.get("/oauth/callback")
def oauth_callback(code: str, state: str):
    if not oauth_state.verify_state(state, "login"):
        raise HTTPException(400, "Invalid or expired OAuth state")

    flow = auth.build_flow(config.SCOPES_PRIMARY, config.REDIRECT_URI, state=state)
    identity = auth.exchange_code_for_identity(flow, code)

    cookie_value = session.create_session_cookie(identity["sub"], identity["email"], identity["refresh_token"])
    resp = RedirectResponse("/dashboard")
    resp.set_cookie(
        config.SESSION_COOKIE_NAME, cookie_value,
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(config.SESSION_COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------
# auth: add a secondary drive
# ---------------------------------------------------------------------
@app.get("/connect-drive")
def connect_drive(request: Request):
    require_session(request)
    flow = auth.build_flow(config.SCOPES_SECONDARY, config.SECONDARY_REDIRECT_URI, state=oauth_state.make_state("secondary"))
    url = auth.get_authorization_url(flow, force_account_chooser=True)
    return RedirectResponse(url)


@app.get("/oauth/callback/secondary")
def oauth_callback_secondary(request: Request, code: str, state: str):
    sess = require_session(request)
    if not oauth_state.verify_state(state, "secondary"):
        raise HTTPException(400, "Invalid or expired OAuth state")

    flow = auth.build_flow(config.SCOPES_SECONDARY, config.SECONDARY_REDIRECT_URI, state=state)
    identity = auth.exchange_code_for_identity(flow, code)

    if identity["sub"] == sess["sub"]:
        raise HTTPException(400, "That's already your primary account -- pick a different Google account.")

    primary_client = DriveClient(sess["refresh_token"], config.SCOPES_PRIMARY, account_key=sess["sub"])
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
@app.post("/upload")
async def upload(request: Request, files: list[UploadFile], folder: str = Form("")):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)
    folder = vault.normalize_path(folder)

    payloads = []
    for f in files:
        key = f"{folder}/{f.filename}" if folder else f.filename
        payloads.append((key, await f.read()))

    try:
        grouped_chunks = distributor.upload_many(clients, payloads)
    except ValueError as e:
        raise HTTPException(400, str(e))

    for key, data in payloads:
        vault.record_file(v, key, len(data), grouped_chunks[key])

    primary_client = clients[sess["sub"]]
    vault.save_vault(primary_client, v)
    return RedirectResponse(f"/dashboard?folder={folder}", status_code=303)


@app.get("/download/{filename:path}")
def download(request: Request, filename: str):
    sess = require_session(request)
    clients, v = build_clients_and_vault(sess)

    info = v["files"].get(filename)
    if not info:
        raise HTTPException(404, "File not found")

    display_name = filename.split("/")[-1]
    data = distributor.download_with_chunks(clients, info["chunks"])
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{display_name}"',
            "Content-Length": str(len(data)),
        },
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
