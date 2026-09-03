# Drive Vault

Turns several Google Drive accounts into one storage pool. No database —
every user's connected accounts and file index live encrypted inside their
own primary Drive (in the hidden `appDataFolder`). Multi-user by design:
anyone can sign in with their own Google account and connect up to 10 of
their own drives; nothing is ever shared between users.

**How it decides where files go:** on upload, it checks free space on all
your connected drives in parallel, then puts the file on whichever has the
most room. If a file is bigger than any single drive's free space, it gets
split into chunks sized to fit, spread across drives, and uploaded
concurrently. Downloads fetch all chunks in parallel and reassemble them.

---

## 1. Google Cloud setup (one-time, ~10 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**
   - Scopes: add `.../auth/drive.file` and `.../auth/drive.appdata`
   - Leave the app in **Testing** mode for personal/small-group use (see note below) or publish it if you want it fully public.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs — add both (use `http://localhost:8000/...` for now, you'll add the real deployed URLs after step 2):
     ```
     http://localhost:8000/oauth/callback
     http://localhost:8000/oauth/callback/secondary
     ```
   - Save the **Client ID** and **Client Secret**.

> **About "Testing" mode:** an unverified app can have up to 100 users, but
> each of them will see a "Google hasn't verified this app" warning screen
> before continuing — that's just how Google treats any OAuth app that
> hasn't gone through their (free, but multi-day) verification review. For
> just yourself and friends, click through it once; it's harmless. If you
> want a fully public app with no warning, you'd submit for verification
> later — no code changes needed.

## 2. Deploy to Render (free, no credit card)

1. Push this folder to a GitHub repo.
2. Go to [render.com](https://render.com) → sign up (email only, no card) → **New → Blueprint** → connect your repo. Render will read `render.yaml` automatically.
3. When prompted for environment variables, fill in:
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — from step 1
   - `REDIRECT_URI` — `https://<your-service-name>.onrender.com/oauth/callback`
   - `SECRET_KEY` — Render can auto-generate this (already configured in `render.yaml`)
4. Deploy. Once live, go back to the Google Cloud Console credentials page and add your real Render URLs as authorized redirect URIs:
   ```
   https://<your-service-name>.onrender.com/oauth/callback
   https://<your-service-name>.onrender.com/oauth/callback/secondary
   ```

That's it — the service is free forever on Render's free web tier (it does
spin down after ~15 minutes of no traffic and takes ~30–50s to wake back
up on the next request; fine for a personal tool).

## 3. Using it

- Visit your URL → **Sign in with Google** (this becomes your primary account/identity).
- On the dashboard, click **Connect another drive** to add up to 9 more Google accounts. Google will show an account picker each time.
- Drag files into the upload form — they're distributed across your drives automatically.
- Download/delete from the file table; both work no matter which drive(s) a file actually lives on.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

Run the offline logic tests (no Google credentials needed):
```bash
PYTHONPATH=. python tests/test_logic.py
```

## Architecture notes

- **No database, anywhere.** Each user's vault (connected-drive tokens +
  file index) is an encrypted JSON blob stored in their own primary
  account's `appDataFolder` — invisible in their normal Drive UI, and
  readable only by this app.
- **Stateless server.** Login sessions are a signed, encrypted cookie —
  not a server-side session. Any instance can serve any request, which is
  exactly what a free tier that spins containers up/down wants.
- **Scoped access only.** The app requests `drive.file` (only files it
  creates) and `drive.appdata` (its own hidden config), not full Drive
  access — this avoids Google's costly "restricted scope" security
  assessment that full `drive` access would require.
- **Concurrency.** All Drive REST calls are I/O-bound, so a
  `ThreadPoolExecutor` gives real parallelism despite Python's GIL —
  used for free-space checks across all drives, and for uploading/
  downloading every chunk of every file in a batch simultaneously.

## Known limitations / good next enhancements

- Uploaded file bytes are held in memory during a request. Render's free
  tier has ~512MB RAM, so very large files (multi-GB) would need
  streaming chunked upload instead of the current buffer-then-split
  approach — say the word and I'll add that.
- No file versioning or folder structure yet — everything is a flat
  namespace of filenames per user.
- No progress bar on upload/download (fine for small-medium files, worth
  adding for large ones).
