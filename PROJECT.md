# Drive Vault — Project Overview

Turns several Google Drive accounts into one pooled storage system, with
**no database and no server-side per-user credential storage**. Deployed
on Render's free tier. This document describes the codebase as it
actually exists today (`app/`).

## Core idea

Every Google account has a hidden `appDataFolder` — invisible in the normal
Drive UI, readable only by the OAuth app that created it. That's where all
"server" state actually lives:

- Each user's **vault** (an encrypted JSON blob) sits in their own primary
  account's `appDataFolder`.
- The vault holds: connected secondary-drive refresh tokens (encrypted),
  the virtual folder tree, and the file index (`path → which drive(s)/file
  id(s)/chunk info`).
- Nothing is shared or written to the server's disk — the server is fully
  stateless, which is exactly what a free host that spins containers up/down
  wants.

## How the design evolved

This went through two different auth models before landing where it is now:

1. **Original design:** one shared Google Cloud OAuth app, configured once
   by whoever deploys the service.
2. **Bring-your-own-OAuth (an intermediate version):** every user pasted in
   their *own* Google Cloud Client ID/Secret through a guided onboarding
   flow, so the deployer needed zero Google setup. This was reverted —
   see below.
3. **Back to one shared OAuth app (current):** [`app/config.py`](app/config.py)
   requires `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` as server env vars,
   set once by the deployer. Every route and `DriveClient` call uses them
   directly; there's no per-user credential anywhere.

**Why the reversion:** the bring-your-own model meant a user who lost their
own Client ID/Secret would permanently lose access to their vault *and*
their uploaded files — not just an inconvenience. Google's `appDataFolder`
and `drive.file` scope are both scoped **per OAuth Client ID**, not per
Google account: a different Client ID (even in the same Google Cloud
project) is treated as a completely different application and gets a
blank, empty `appDataFolder`, with no visibility into files the old
Client ID created. There is no way to "look up" the old credentials from
inside the vault to recover, either, since reading the vault itself
requires already being authenticated with the very credentials you'd be
trying to recover. A single shared OAuth app removes the failure mode
entirely: nobody holds a credential they could lose, at the cost of one
shared Drive API quota across all users and Google's ~100-user cap while
the app stays in "Testing" mode (lifted by the standard, free OAuth
verification review once you need more).

Other, independent features added along the way (unaffected by the OAuth
reversion):
- **Folders** (virtual, not real nested Drive folders — see
  [`app/vault.py`](app/vault.py:1) docstring), plus multi-select batch delete.
- **"Import from Drive"** via the client-side Google Picker widget
  (optional, gated on a `GOOGLE_PICKER_API_KEY` env var), so users can pull
  in files that already exist elsewhere in their Google accounts without
  widening the app's own server-side OAuth scope.
- **Access-token caching** ([`app/token_cache.py`](app/token_cache.py)),
  in-memory, keyed by Google account id, so repeat requests within a
  token's ~1 hour lifetime skip re-authenticating with Google entirely.

## Auth flow (as built)

1. **Sign in** (`GET /login` in [`app/main.py`](app/main.py)): builds the
   OAuth authorization URL directly from `config.GOOGLE_CLIENT_ID`/
   `GOOGLE_CLIENT_SECRET` and redirects to Google. No form, no per-user
   input at all.
2. **Google OAuth round trip** (`GET /oauth/callback`): exchanges the code
   for identity + refresh token, bootstraps the vault, and issues the
   session cookie (`dv_session`, 30-day TTL). That becomes the user's
   primary-account identity going forward.
3. **Adding a secondary drive** (`GET /connect-drive` →
   `GET /oauth/callback/secondary`): repeats OAuth using the same shared
   app, authorizing another of the user's own Google accounts, up to
   `MAX_DRIVES_PER_USER` (default 10).
4. **Session = stateless signed+encrypted cookie**
   ([`app/session.py`](app/session.py)) holding just `sub`, `email`, and
   the (encrypted) refresh token. Any server instance can serve any
   request from the cookie alone. There's no separate "pending setup" or
   "remember this device" cookie (both existed briefly during the
   bring-your-own era) — since there's no per-user secret to carry through
   a redirect or remember across a lost session, re-authenticating is
   always just a single "Sign in with Google" click, session or no session.
5. **CSRF protection on the OAuth `state` param**
   ([`app/oauth_state.py`](app/oauth_state.py)) is done by signing it with
   `SECRET_KEY` rather than comparing against server-side state, since there
   is no server-side session store to compare against.

## Encryption ([`app/crypto.py`](app/crypto.py))

Every sensitive value — refresh tokens, the whole vault blob — is
Fernet-encrypted (key derived via SHA-256 of `SECRET_KEY`) before it's
ever written into a cookie or into the Drive `appDataFolder`. Signing
alone (which `itsdangerous` also provides, for tamper-detection) doesn't
stop someone from reading a value straight out of a cookie; encryption does.

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

## Concurrency ([`app/workers.py`](app/workers.py))

All Drive REST calls are I/O-bound, so a `ThreadPoolExecutor` gives real
parallelism despite the GIL. `MAX_WORKERS` (default 4, deliberately
conservative) caps this — see "Known limitations" below for why.
Google's client library is **not thread-safe** at the connection level, so
every concurrent call gets its own independent `DriveClient` via
`fresh_copy()` rather than sharing one across threads
([`app/drive_client.py`](app/drive_client.py:58)).

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
| [`app/config.py`](app/config.py) | Env-driven settings, including the shared `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` |
| [`app/auth.py`](app/auth.py) | Google OAuth2 flow helpers (client_id/secret passed in, always the shared app's) |
| [`app/session.py`](app/session.py) | Signed+encrypted session cookie (sub/email/refresh_token only) |
| [`app/oauth_state.py`](app/oauth_state.py) | Signed CSRF state for the OAuth redirect |
| [`app/crypto.py`](app/crypto.py) | Fernet encrypt/decrypt helpers |
| [`app/vault.py`](app/vault.py) | The "database": vault schema, folder tree, file index |
| [`app/drive_client.py`](app/drive_client.py) | Thin wrapper over one Drive account's REST calls |
| [`app/distributor.py`](app/distributor.py) | Chunk-planning + parallel upload/download/delete |
| [`app/workers.py`](app/workers.py) | `ThreadPoolExecutor` helpers |
| [`app/token_cache.py`](app/token_cache.py) | In-memory access-token cache, keyed by Google sub |
| [`app/templates/`](app/templates/) | `welcome.html` (single "Sign in with Google" button), `dashboard.html` (main UI) |

## Known limitations (from the README, still current)

- **Upload/download bytes are buffered fully in memory**, not streamed —
  real risk of OOM on Render's 512MB free tier with large/concurrent
  transfers (has happened in testing). `MAX_WORKERS` is capped at 4 because
  of this. A true streaming fix (straight into Drive's resumable-upload API)
  is scoped but not built.
- **No file versioning** — re-uploading the same name overwrites the index
  entry; the old Drive object is orphaned rather than reused/deleted.
- **Google's ~100-user Testing-mode cap** applies to the whole deployment
  (not per-user, since everyone shares one OAuth app) until the deployer
  publishes it via Google's standard OAuth verification.

## Tests (`tests/`, offline — no network/Google credentials needed)

`test_logic.py`, `test_thread_safety.py`, `test_token_cache.py`,
`test_folders.py`, `test_folder_and_batch_routes.py`,
`test_session_and_vault.py`, `test_onboarding_flow.py`,
`test_dashboard_render.py`, `test_upload_concurrency_regression.py` —
cover encryption round-trips, session/cookie handling, chunk-planning
math, folder path logic, and the shared-OAuth-app login flow.

Run with:
```bash
PYTHONPATH=. python -m pytest tests/ -q
```
