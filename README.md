# Drive Vault

Turns several Google Drive accounts into one storage pool. No database —
every user's connected accounts and file index live encrypted inside their
own primary Drive (in the hidden `appDataFolder`). Multi-user by design:
anyone can sign in and connect up to 10 of their own drives; nothing is
ever shared between users.

**One shared Google OAuth app serves everyone.** The person deploying this
app creates a single Google Cloud OAuth Client ID/Secret once (see the
setup guide below) and configures it as an environment variable. Every
user just clicks "Sign in with Google" — nobody ever sees, enters, or has
to safeguard a Client ID/Secret. This is a deliberate trade-off: earlier
versions of this app had each user bring their own OAuth app, but losing
that credential turned out to permanently orphan that person's vault and
files (Google's `appDataFolder` and `drive.file` access are both scoped
per OAuth Client ID, not per Google account — a new Client ID can't see
what an old one created). A single shared app removes that risk entirely,
at the cost of one shared Drive API quota across all users of this
deployment, and needing the deployer to own one small Google Cloud project.

**How it decides where files go:** on upload, it checks free space on all
your connected drives in parallel, then puts the file on whichever has the
most room. If a file is bigger than any single drive's free space, it gets
split into chunks sized to fit, spread across drives, and uploaded
concurrently. Downloads fetch all chunks in parallel and reassemble them.

---

## One-time setup: create the shared Google OAuth app

Do this once, before deploying (or before your first local run).

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a new project (any name).
2. **APIs & Services → Library** → search **Google Drive API** → **Enable**.
3. **APIs & Services → Google Auth Platform → Get started.**
   - App name: anything. User support email: yours.
   - Audience: **External**. While the app stays in **Testing** mode (the
     default), only the test users you list here can sign in — up to 100.
     If you expect more than ~100 people to use this deployment, you'll
     eventually need to publish the app, which requires Google's standard
     (free, non-restricted) OAuth verification review.
   - On **Data access**, add scopes `drive.file` and `drive.appdata`.
4. **Clients** tab → **Create Client** → **Web application**.
   - Under **Authorized redirect URIs**, add both, replacing the host with
     wherever you'll deploy (or `localhost:8000` for local dev):
     ```
     https://YOUR-DEPLOYED-HOST/oauth/callback
     https://YOUR-DEPLOYED-HOST/oauth/callback/secondary
     ```
   - Click **Create**, then copy the **Client ID** and **Client Secret** —
     the secret is only ever shown once.

You'll paste both into the deploy step below (or your local `.env`).

## Deploy to Render (free, no credit card)

1. Push this folder to a GitHub repo.
2. Go to [render.com](https://render.com) → sign up (email only, no card) → **New → Blueprint** → connect your repo. Render reads `render.yaml` automatically, generates `SECRET_KEY` for you, and prompts you for `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
3. Paste in the Client ID/Secret from the setup above, then deploy.

The service is free forever on Render's free web tier (it spins down
after ~15 minutes of no traffic and takes ~30–50s to wake back up on the
next request; fine for a personal tool).

## Using it (what each person sees)

1. Visit your deployed URL and click **Sign in with Google**.
2. From then on, that's your identity. A signed-in session lasts 30 days;
   after that (or if you clear cookies), signing in again is still just
   one click — there's nothing to remember or retype.
3. On the dashboard, **Connect another drive** adds up to 9 more Google
   accounts of your own into the same pool.
4. Drag files into the upload form — they're distributed across your
   drives automatically.
5. **Folders**: click **+ New folder** to create one, click into it to
   navigate, and anything you upload while inside a folder lands there.
   Folders are a purely virtual/app-level concept — see the architecture
   notes below for what that means in practice.
6. **Multi-delete**: check the boxes next to files and click **Delete
   selected** to remove several at once.
7. **Import from Drive**: if the deployer set up `GOOGLE_PICKER_API_KEY`,
   this button opens Google's own file browser so you can pull in files
   that already exist in any of your Google accounts (not just files this
   app created).
8. Download/delete from the file table; both work no matter which
   drive(s) a file actually lives on.

## How this stays safe (read this if you're not sure)

- **Nothing is stored on the server, ever.** No database, nothing written
  to disk. Your login session is a signed, encrypted cookie in your
  browser (30 days); your file index and connected-drive list live
  encrypted inside your own Google Drive's hidden `appDataFolder`.
- **Scoped access only.** The shared app requests `drive.file` (only
  files it creates) and `drive.appdata` (its own hidden config) — never
  your existing Drive contents, and never the costly "restricted scope"
  security assessment that full `drive` access would require.
- Every sensitive value — refresh tokens — is encrypted (via
  `SECRET_KEY`) before it's ever written into a cookie or the vault.
  Signing alone (which the app also does, to detect tampering) wouldn't
  stop someone from reading the value; encryption does.
- The two things that are genuinely global to this deployment are
  `SECRET_KEY` (encrypts/signs cookies and vault contents) and the shared
  `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (the one OAuth app everyone
  authenticates through). The person running the server controls all
  three; none of them by themselves grant access to any specific user's
  Drive without also having that user's refresh token, which never leaves
  their own cookie/vault.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GOOGLE_CLIENT_ID/SECRET (see setup guide above)
                        # and SECRET_KEY, generated with:
                        #   python -c "import secrets; print(secrets.token_urlsafe(32))"
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

Visit `http://localhost:8000` and click **Sign in with Google** — make
sure `http://localhost:8000/oauth/callback` and
`http://localhost:8000/oauth/callback/secondary` are both in your OAuth
client's authorized redirect URIs.

Run the offline tests (no Google credentials or network needed):
```bash
PYTHONPATH=. python -m pytest tests/ -q
```

## Architecture notes

- **No database, anywhere.** Each user's vault (connected-drive tokens
  and file index) is an encrypted JSON blob stored in their own primary
  account's `appDataFolder` — invisible in their normal Drive UI, and
  readable only by this app's OAuth Client ID.
- **One shared OAuth app.** `config.py` holds `GOOGLE_CLIENT_ID` /
  `GOOGLE_CLIENT_SECRET` as required environment variables; every route
  and `DriveClient` call uses them directly rather than taking per-user
  credentials as parameters. This is why the session cookie only needs to
  carry `sub`/`email`/`refresh_token` — there's no per-user secret to
  encrypt alongside them, and no separate "remember this device" cookie
  is needed either, since re-authenticating is always a single "Sign in
  with Google" click regardless of how long ago the session expired.
- **Stateless server.** Login sessions are a signed, encrypted cookie —
  not a server-side session. Any instance can serve any request, which is
  exactly what a free tier that spins containers up/down wants.
- **Concurrency.** All Drive REST calls are I/O-bound, so a
  `ThreadPoolExecutor` gives real parallelism despite Python's GIL —
  used for free-space checks across all drives, and for uploading/
  downloading every chunk of every file in a batch simultaneously.
  Google's client library is explicitly not thread-safe at the
  connection level, so every concurrent call gets its own independent
  connection (`DriveClient.fresh_copy()`) rather than sharing one.
- **Access-token caching.** Each connected account's short-lived access
  token is cached in memory (keyed by the account's own Google id) across
  requests, so repeat requests within the token's ~1 hour lifetime skip
  re-authenticating with Google entirely. This is why folder navigation
  and file operations feel fast after the first request in a session.
- **Folders are virtual, not real Drive folders.** A given folder's files
  are usually scattered across several physical drives (that's the whole
  point of the pooling), so there's no single real Drive folder to mirror
  them into. Instead, a folder is just a path prefix tracked in the
  vault's own metadata. One visible side effect: if you open a file
  directly in its actual Google Drive account, its name there is the full
  virtual path (e.g. `docs/receipts/jan.pdf`) rather than a real nested
  folder — a bit unusual to look at, but functionally harmless.
- **Importing existing files uses Google Picker, not a broader scope.**
  `drive.readonly` and even `drive.metadata.readonly` are classified by
  Google as *restricted* scopes requiring an annual paid security
  assessment — not worth it for this app. Instead, "Import from Drive"
  loads Google's own Picker widget client-side; whatever the user selects
  there is downloaded directly by the browser (using a short-lived token
  scoped only to `drive.file`) and then fed into the normal upload
  pipeline. The app's own server-side scope never changes. The one gap
  this leaves: Picker lets you *browse into* folders but doesn't support
  picking a whole folder recursively — you can multi-select many files
  at once instead. Native Google Docs/Sheets/Slides are auto-exported to
  `.docx`/`.xlsx`/`.pptx` on the way in, since those formats have no raw
  file bytes to download directly.

## Known limitations / good next enhancements

- Uploaded file bytes are held in memory during a request rather than
  streamed. On Render's free tier (512MB RAM), this can OOM-kill the
  instance with either large files or several files uploading at once
  — this has actually happened in testing. `MAX_WORKERS` defaults to a
  conservative `4` specifically because of this (down from `10`); lower
  it further via env var if you still see out-of-memory restarts in
  Render's Events tab, or raise it if you deploy somewhere with more RAM.
  A proper fix (streaming straight from the upload into Drive's
  resumable-upload API, so peak memory stops scaling with file size at
  all) is a real, scoped piece of work — say the word and I'll build it.
- No file versioning yet — re-uploading the same name overwrites the
  index entry (the old Drive object becomes orphaned rather than reused).
- While the shared OAuth app stays in Google's default **Testing** mode,
  only up to 100 test users (added manually in Google Cloud Console) can
  sign in at all. Growing past that requires publishing the app, which
  needs Google's standard OAuth verification (free, but a review queue —
  not the paid "restricted scope" security assessment, since this app's
  scopes don't require that tier).
