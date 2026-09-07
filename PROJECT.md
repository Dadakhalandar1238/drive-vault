# Drive Vault — Project Overview

Turns several Google Drive accounts into one pooled storage system, with
**no database and no server-side credential storage**. Deployed on Render's
free tier. This document describes the codebase as it actually exists today
(`app/`), not just the original design conversation — the implementation has
moved past that first design in a few important ways, noted below.

## Core idea

Every Google account has a hidden `appDataFolder` — invisible in the normal
Drive UI, readable only by the app that created it. That's where all "server"
state actually lives:

- Each user's **vault** (an encrypted JSON blob) sits in their own primary
  account's `appDataFolder`.
- The vault holds: connected secondary-drive refresh tokens (encrypted), an
  encrypted backup of the user's own OAuth Client ID/Secret, the virtual
  folder tree, and the file index (`path → which drive(s)/file id(s)/chunk info`).
- Nothing is shared or written to the server's disk — the server is fully
  stateless, which is exactly what a free host that spins containers up/down
  wants.

## How the design evolved from the original chat

The original conversation assumed **one shared Google Cloud OAuth app**
(a single Client ID/Secret configured once by whoever deploys the service),
with `drive.file` + `drive.appdata` scopes. The codebase went a step further:

- **Bring-your-own-OAuth.** There is no global Client ID/Secret anywhere —
  see [`app/config.py`](app/config.py:5). Every user pastes in their *own*
  free Google Cloud project's credentials through a guided setup screen on
  first visit ([`app/templates/welcome.html`](app/templates/welcome.html)),
  computed redirect URIs and all. This means the person deploying the app
  needs zero Google setup, and no one user's credentials ever touch another
  user's session or the server's disk. `DriveClient`, `auth.build_flow`, and
  every route take `client_id`/`client_secret` as explicit parameters — by
  design, there's no fallback global value.
- **Folders** (virtual, not real nested Drive folders — see
  [`app/vault.py`](app/vault.py:1) docstring) were added, along with
  multi-select batch delete.
- **"Import from Drive"** was added via the client-side Google Picker widget
  (optional, gated on a `GOOGLE_PICKER_API_KEY` env var) so users can pull in
  files that already exist elsewhere in their Google accounts — without
  widening the app's own OAuth scope to a Google-restricted one.
- **Access-token caching** ([`app/token_cache.py`](app/token_cache.py)) was
  added in-memory, keyed by Google account id, so repeat requests within a
  token's ~1 hour lifetime skip re-authenticating with Google entirely.

## Auth flow (as built)

1. **First-time setup** (`POST /start-setup` in
   [`app/main.py`](app/main.py:197)): user enters their own Client ID/Secret
   (and optionally an email). These sit in a short-lived, encrypted
   `dv_pending_setup` cookie (15 min TTL) just long enough to survive the
   redirect to Google and back — never written to disk.
2. **Google OAuth round trip** (`GET /oauth/callback`): exchanges the code
   for identity + refresh token, bootstraps the vault, stores an encrypted
   backup of the Client ID/Secret inside it (`vault.set_oauth_client`), and
   issues the real session cookie (`dv_session`, 30-day TTL). That becomes
   the user's primary-account identity going forward.
3. **Adding a secondary drive** (`GET /connect-drive` →
   `GET /oauth/callback/secondary`): repeats OAuth using the *same*
   Client ID/Secret already on file for the session — one small Google Cloud
   project authorizes as many of the user's own accounts as needed, up to
   `MAX_DRIVES_PER_USER` (default 10).
4. **Session = stateless signed+encrypted cookie**
   ([`app/session.py`](app/session.py)) holding `sub`, `email`, the
   (encrypted) refresh token, and the (encrypted) Client Secret. Any server
   instance can serve any request from the cookie alone.
5. **Remembered device (added after initial build, later upgraded):** a
   second, separate cookie (`dv_remember`, 1-year TTL) carries the Client
   ID/Secret *and* the refresh token from the last successful login.
   `GET /continue` uses that refresh token directly: a cheap
   `get_storage_info()` call forces a real token refresh, and if it
   succeeds, a new session is created straight from it — **no redirect
   to Google at all**, since nothing new is being authorized. Only if
   that refresh token no longer works (revoked, or expired from months
   of disuse) does it fall back to `_redirect_to_google_login()` — the
   same helper `/start-setup` uses — still with the remembered Client
   ID/Secret, so nothing to retype even on the fallback path.
   `GET /forget-device` clears both cookies. This exists because the
   server keeps no state of its own: the refresh token has to live
   *somewhere* client-side for a lapsed session to resume without
   re-prompting consent, and the remember cookie is the only place that
   isn't the vault (which needs a live Drive connection to read in the
   first place — a chicken-and-egg problem the remember cookie sidesteps).
5. **CSRF protection on the OAuth `state` param**
   ([`app/oauth_state.py`](app/oauth_state.py)) is done by signing it with
   `SECRET_KEY` rather than comparing against server-side state, since there
   is no server-side session store to compare against.

## Encryption ([`app/crypto.py`](app/crypto.py))

Every sensitive value — refresh tokens, Client Secret, the whole vault
blob — is Fernet-encrypted (key derived via SHA-256 of `SECRET_KEY`) before
it's ever written into a cookie or into the Drive `appDataFolder`. Signing
alone (which `itsdangerous` also provides, for tamper-detection) doesn't stop
someone from reading a value straight out of a cookie; encryption does.

## Storage distribution ([`app/distributor.py`](app/distributor.py))

On upload:
1. Query free space on every connected drive in parallel (one round of
   `about.get()` calls).
2. For each file: if any single drive has room, place it on whichever has
   the **most** free space (spreads wear evenly rather than always filling
   drive #1).
3. If no single drive fits, split the file into chunks sized to each drive's
   remaining free space (largest-free-first), spread across drives.
4. Flatten every chunk of every file in the batch into one task list and
   upload all of them concurrently in a single thread pool — so a multi-file
   upload's chunks land on different drives *simultaneously*, not one file
   at a time.

Downloads reverse this: every chunk of a file is fetched concurrently and
reassembled in offset order. Deletes fan out the same way.

**Everything above is planning only — no file bytes involved.**
`upload_many()` takes `(filename, fd, size)`, not `(filename, bytes)`:
`main.py`'s upload route never calls `await file.read()`; it just gets the
already-parsed `UploadFile`'s own file descriptor (Starlette spools
anything past a small threshold to a real temp file on disk, so
`.fileno()` is always available) and its size (via seek/tell). Each
planned chunk is read from that fd at its own offset, on demand, at
upload time — see `DriveClient.upload_from_fd()` below. Downloads
mirror this: every chunk writes directly to its correct offset in one
shared temp file (`DriveClient.download_to_fd()`), which is then
streamed back to the browser off disk and deleted. This is what makes a
20GB upload/download cost the same handful of megabytes of RAM as a
20KB one — see the README's matching architecture note for the full
rationale (this replaced an earlier full-buffering implementation that
risked OOM on exactly this).

## Concurrency ([`app/workers.py`](app/workers.py))

All Drive REST calls are I/O-bound, so a `ThreadPoolExecutor` gives real
parallelism despite the GIL. `MAX_WORKERS` (default 10) caps this — safe
at that default specifically because of the streaming design above: peak
memory is `MAX_WORKERS × drive_client.TRANSFER_CHUNK_SIZE` (8 MiB), not
`MAX_WORKERS × file_size` like before. Google's client library is **not
thread-safe** at the connection level, so every concurrent call gets its
own independent `DriveClient` via `fresh_copy()` rather than sharing one
across threads ([`app/drive_client.py`](app/drive_client.py:58)).

## Scopes

`drive.file` (only files the app itself creates) + `drive.appdata` (hidden
vault storage) — deliberately **not** full `drive` access, which would
trigger Google's costly "restricted scope" security assessment. Importing
existing files sidesteps this entirely by using the client-side Google
Picker widget instead of a broader read scope.

## Stack

- **Backend:** Python 3.12 + FastAPI + Uvicorn
- **Google integration:** `google-api-python-client`, `google-auth-oauthlib`
- **Crypto:** `cryptography` (Fernet), `itsdangerous` (signed cookies)
- **Frontend:** server-rendered Jinja2 templates + vanilla JS (drag/drop
  upload, async storage-gauge refresh, Google Picker integration) — no JS
  framework/build step
- **Hosting:** Render.com free web tier via [`render.yaml`](render.yaml) /
  [`Dockerfile`](Dockerfile) — no credit card required anywhere in the stack;
  spins down after ~15 min idle, ~30–50s cold start on next request

## File map

| File | Responsibility |
|---|---|
| [`app/main.py`](app/main.py) | FastAPI routes: pages, auth, upload/download/delete, folders |
| [`app/config.py`](app/config.py) | Env-driven settings; no global OAuth credentials |
| [`app/auth.py`](app/auth.py) | Google OAuth2 flow (per-user client id/secret) |
| [`app/session.py`](app/session.py) | Signed+encrypted session & pending-setup cookies |
| [`app/oauth_state.py`](app/oauth_state.py) | Signed CSRF state for the OAuth redirect |
| [`app/crypto.py`](app/crypto.py) | Fernet encrypt/decrypt helpers |
| [`app/vault.py`](app/vault.py) | The "database": vault schema, folder tree, file index |
| [`app/drive_client.py`](app/drive_client.py) | Thin wrapper over one Drive account's REST calls |
| [`app/distributor.py`](app/distributor.py) | Chunk-planning + parallel upload/download/delete |
| [`app/workers.py`](app/workers.py) | `ThreadPoolExecutor` helpers |
| [`app/token_cache.py`](app/token_cache.py) | In-memory access-token cache, keyed by Google sub |
| [`app/templates/`](app/templates/) | `welcome.html` (onboarding), `dashboard.html` (main UI) |

## Known limitations (from the README, still current)

- **No file versioning** — re-uploading the same name overwrites the index
  entry; the old Drive object is orphaned rather than reused/deleted.
- **Session recovery is per-browser-cookie only** — no cross-device account
  system, no email/password recovery, since there's no database by design.
  The remembered-device cookie reduces a lapsed session (on the *same*
  browser) to a **silent** resume (no Google redirect at all, since it
  reuses the stored refresh token directly — see `/continue` in
  `app/main.py`), falling back to a real Google round trip only if that
  refresh token has actually stopped working. A genuinely new browser/
  device still needs the Client ID/Secret typed in once.

## Tests (`tests/`, offline — no network/Google credentials needed)

`test_logic.py`, `test_thread_safety.py`, `test_token_cache.py`,
`test_folders.py`, `test_folder_and_batch_routes.py`,
`test_byo_oauth_credentials.py`, `test_onboarding_flow.py`,
`test_dashboard_render.py`, `test_upload_concurrency_regression.py`,
`test_streaming_transfer.py` — cover encryption round-trips,
session/cookie handling, chunk-planning math, folder path logic, the
bring-your-own-OAuth onboarding flow, and (that last file) byte-level
correctness of the fd-based streaming upload/download primitives — real
temp files and real `os.pread`/`os.pwrite`, not just mocked call
assertions, since a silent off-by-one there would corrupt files.

Run with:
```bash
PYTHONPATH=. python -m pytest tests/ -q
```
