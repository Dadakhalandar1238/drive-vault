# Drive Vault

Turns several Google Drive accounts into one storage pool. No database —
every user's connected accounts and file index live encrypted inside their
own primary Drive (in the hidden `appDataFolder`). Multi-user by design:
anyone can sign in and connect up to 10 of their own drives; nothing is
ever shared between users.

**Nobody shares Google credentials.** This app has no Google API access
of its own. Each person who signs in brings their own free Google Cloud
project (their own Client ID/Secret) through a guided setup screen built
into the app — the person deploying this doesn't need a Google Cloud
project at all, and nobody's Client ID or Secret is ever stored on the
server. See "How credentials are handled" below for exactly what that means.

**How it decides where files go:** on upload, it checks free space on all
your connected drives in parallel, then puts the file on whichever has the
most room. If a file is bigger than any single drive's free space, it gets
split into chunks sized to fit, spread across drives, and uploaded
concurrently. Downloads fetch all chunks in parallel and reassemble them.

---

## Deploy to Render (free, no credit card)

There is nothing Google-related to configure before deploying — that
happens per-user, in the app itself, after it's live.

1. Push this folder to a GitHub repo.
2. Go to [render.com](https://render.com) → sign up (email only, no card) → **New → Blueprint** → connect your repo. Render reads `render.yaml` automatically and generates `SECRET_KEY` for you.
3. Deploy. That's it — the only required environment variable is `SECRET_KEY`, and the blueprint already generates one.

The service is free forever on Render's free web tier (it spins down
after ~15 minutes of no traffic and takes ~30–50s to wake back up on the
next request; fine for a personal tool).

## Deploy to Oracle Cloud (OCI, free forever, no cold starts) via Terraform

OCI's "Always Free" tier includes an Arm-based Ampere A1 compute instance
(up to 4 OCPUs / 24 GB RAM) that runs indefinitely — no idle spin-down like
Render. The one trade-off: unlike Render, **Oracle requires a valid
credit/debit card at signup** for identity verification, even though
Always-Free resources are never charged.

The [`terraform/`](terraform/) directory provisions everything —
networking, the compute instance, and (via cloud-init) Docker, the app
itself, and [Caddy](https://caddyserver.com/) as a reverse proxy that
gets you free, automatically-renewing HTTPS. Nothing Google-related is
configured here either — same as Render, that happens per-user, in the
app itself.

1. **Create an OCI account** at [cloud.oracle.com](https://cloud.oracle.com) (needs a card for verification, Always-Free usage itself is never charged).
2. **Set up local API access** — install the [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm), then run:
   ```bash
   oci setup config
   ```
   This walks you through generating an API signing key and writes
   `~/.oci/config`, which Terraform reuses automatically.
3. **Install Terraform** ([terraform.io/downloads](https://www.terraform.io/downloads)) and an SSH key pair if you don't already have one (`ssh-keygen`).
4. **Configure and deploy:**
   ```bash
   cd terraform
   cp terraform.tfvars.example terraform.tfvars
   # edit terraform.tfvars: at minimum set region + compartment_ocid
   # (Console -> Profile -> Tenancy for your tenancy OCID)
   terraform init
   terraform apply
   ```
5. Wait a few minutes for the instance to boot and the app to build
   (`docker compose up -d --build` runs automatically via cloud-init),
   then visit the `app_url` Terraform prints — either your own domain, if
   you set the `domain` variable and pointed its DNS A record at the
   printed `public_ip`, or a free `nip.io` hostname derived from that IP
   automatically otherwise (no DNS setup needed).
6. To pull in a future code update, SSH in (`terraform output ssh_command`) and run `/opt/drive-vault/update.sh`.
7. `terraform destroy` tears everything down when you're done.

## Using it (what each person sees)

1. Visit your deployed URL. If you're new, you'll land on a setup screen
   with a step-by-step guide to creating a free Google Cloud project and
   getting a Client ID/Secret — the exact redirect URIs to paste in are
   shown for you, computed from your actual deployed URL, so there's no
   guessing. Takes about 10 minutes, once.
2. Paste your Client ID and Secret into the form and continue — this
   kicks off Google's normal sign-in screen, using *your own* OAuth app.
3. From then on, that's your identity. Anyone who already has a signed-in
   session in their browser skips straight to the dashboard. Once your
   session eventually expires (or you clear cookies), you won't be asked
   to retype your Client ID/Secret either — the same browser shows a
   one-click **Continue to Google Sign-In** button instead, using a
   separate, longer-lived (1 year) remembered-device cookie. "Not you? Use
   a different Google account" on that screen clears it if you ever need
   the manual form again (shared computer, switching projects, etc.).
4. On the dashboard, **Connect another drive** adds up to 9 more Google
   accounts — using the *same* Client ID/Secret you already entered, since
   one small Google Cloud project can authorize as many of your own
   accounts as you like.
5. Drag files into the upload form — they're distributed across your
   drives automatically.
6. **Folders**: click **+ New folder** to create one, click into it to
   navigate, and anything you upload while inside a folder lands there.
   Folders are a purely virtual/app-level concept — see the architecture
   notes below for what that means in practice.
7. **Multi-delete**: check the boxes next to files and click **Delete
   selected** to remove several at once.
8. **Import from Drive**: if the deployer set up `GOOGLE_PICKER_API_KEY`,
   this button opens Google's own file browser so you can pull in files
   that already exist in any of your Google accounts (not just files this
   app created).
9. Download/delete from the file table; both work no matter which
   drive(s) a file actually lives on.

## How credentials are handled (read this if you're not sure it's safe)

- **The server never stores your Client ID/Secret anywhere at rest.**
  There's no database, and nothing is written to the server's disk. Your
  credentials exist in exactly three places, all in your own browser or
  your own Drive: the session cookie (30 days), a separate longer-lived
  "remember this device" cookie (1 year, so a lapsed session doesn't force
  you back to the manual form), and an encrypted blob inside your own
  Google Drive's hidden `appDataFolder` (so a copy survives even if you
  clear cookies entirely — restoring it still requires you to re-enter
  your Client ID/Secret once, since reading your Drive requires being
  authenticated to it first).
- While you're mid-setup (after submitting the form, before Google
  redirects you back), your credentials sit briefly in a short-lived
  cookie that expires after 15 minutes and is deleted the moment setup
  finishes.
- Every sensitive value — refresh tokens, your Client Secret — is
  encrypted (via `SECRET_KEY`) before it's ever written into a cookie or
  the vault. Signing alone (which the app also does, to detect tampering)
  wouldn't stop someone from reading the value; encryption does.
- The one thing that's genuinely global to this deployment is
  `SECRET_KEY` itself, which the person running the server controls. It's
  used only to encrypt/sign cookies and vault contents — it can't be used
  to access anyone's Drive by itself.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # only SECRET_KEY is required; generate one with:
                        #   python -c "import secrets; print(secrets.token_urlsafe(32))"
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

Visit `http://localhost:8000` and follow the in-app setup guide the same
way a deployed user would — the redirect URIs it shows you will correctly
say `http://localhost:8000/...`.

Run the offline tests (no Google credentials or network needed):
```bash
PYTHONPATH=. python -m pytest tests/ -q
```

## Architecture notes

- **No database, anywhere.** Each user's vault (connected-drive tokens,
  their own OAuth Client ID/Secret backup, and file index) is an
  encrypted JSON blob stored in their own primary account's
  `appDataFolder` — invisible in their normal Drive UI, and readable only
  by this app.
- **Bring-your-own OAuth app.** Every user supplies their own Google
  Cloud Client ID/Secret through a guided setup flow. A short-lived
  encrypted cookie carries those credentials through the OAuth redirect
  round trip; the resulting session cookie carries them for as long as
  the session lasts. `DriveClient`, `auth.build_flow`, and every route
  take client_id/client_secret as explicit parameters — there is no
  global config value to fall back to, by design.
- **Stateless server.** Login sessions are a signed, encrypted cookie —
  not a server-side session. Any instance can serve any request, which is
  exactly what a free tier that spins containers up/down wants.
- **Remembered-device cookie.** A second, separate encrypted cookie
  (`dv_remember`, 1-year TTL) carries the same Client ID/Secret as the
  session cookie but outlives it, since Google's refresh-token grant still
  requires the original client_id/client_secret to redeem — there's no way
  to recover a lost session's refresh token without them. It's set (and
  refreshed) on every successful `/oauth/callback`, and checked by `/` to
  decide whether to show the manual credential form or a one-click
  "Continue to Google Sign-In" (`/continue`) that skips straight to
  Google's consent screen. `/forget-device` clears both cookies.
- **Scoped access only.** The app requests `drive.file` (only files it
  creates) and `drive.appdata` (its own hidden config), not full Drive
  access — this avoids Google's costly "restricted scope" security
  assessment that full `drive` access would require.
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
- "Existing user" recognition is per-browser (cookies), not a
  cross-device account system, since there's no database. The
  remembered-device cookie means a lapsed session on the *same* browser
  reduces to a one-click continue, but a genuinely new browser or device
  still needs the Client ID/Secret typed in once — there's no
  email/password recovery flow, by design.
