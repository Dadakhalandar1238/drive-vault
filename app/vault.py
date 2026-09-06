"""
The vault is the *entire database* for this app -- and it lives inside
the user's own Google Drive, not on our server. Schema:

{
  "accounts": {
    "<google_sub>": {"email": "...", "refresh_token": "<encrypted>", "label": "Drive 2"}
  },
  "folders": ["docs", "docs/receipts"],
  "files": {
    "<full/virtual/path.ext>": {
      "size": 12345,
      "uploaded_at": "2026-09-03T12:00:00Z",
      "chunks": [
        {"account": "<google_sub>", "file_id": "...", "offset": 0, "size": 12345}
      ]
    }
  }
}

Folders are a purely virtual/app-level concept, not real nested Drive
folders -- a "folder" here is just a path prefix on file keys, tracked
in "folders" so empty folders can still exist and be browsed. This is
deliberate: a folder's files are typically scattered across several
physical drives, so there's no single real Drive folder to mirror them
into anyway. One side effect worth knowing: the file's underlying name
in the real Drive account is the full virtual path (e.g. "docs/a.pdf"),
so if you go look at the raw file in Drive's own UI, you'll see that
full path as a literal filename rather than real nested folders.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .crypto import decrypt, encrypt
from .drive_client import DriveClient

logger = logging.getLogger(__name__)

EMPTY_VAULT = {"accounts": {}, "folders": [], "files": {}}


def load_vault(primary_client: DriveClient) -> dict:
    raw = primary_client.read_vault_raw()
    v = None
    if raw is not None:
        try:
            v = json.loads(decrypt(raw))
        except Exception as e:
            # Most commonly means this vault was encrypted under a
            # different SECRET_KEY than the server is running with now --
            # e.g. testing locally and against a deployed instance with
            # the same Google account, or a key rotation. There's no way
            # to recover the old index without the old key here and now,
            # so rather than crash, start fresh: the user's actual Drive
            # files are untouched, only our tracking of them resets.
            #
            # No exc_info here on purpose -- this is a handled, recovered
            # situation, not a crash, and a full traceback in the logs
            # reads as one even though the request succeeds regardless.
            logger.warning(
                "Could not decrypt existing vault (%s: %s) -- starting a "
                "fresh one. This usually means SECRET_KEY changed since "
                "this vault was last saved.",
                type(e).__name__, e,
            )
            _backup_undecryptable_vault(primary_client, raw)
            v = None
    if v is None:
        v = json.loads(json.dumps(EMPTY_VAULT))  # deep copy
    v.setdefault("folders", [])  # migrates vaults saved before folders existed
    # A vault saved by an earlier, bring-your-own-OAuth version of this app
    # may still carry a leftover "oauth_client" key -- harmless, just unused.
    return v


def _backup_undecryptable_vault(primary_client: DriveClient, raw_ciphertext: str) -> None:
    """Preserves an undecryptable vault before it gets overwritten by a
    fresh one, so a SECRET_KEY mismatch is recoverable later (by someone
    who still has the old key) rather than silently destroyed. Best
    effort: if this itself fails, we still proceed with a fresh vault --
    losing the backup is better than crashing the whole login."""
    try:
        backup_name = f"vault.enc.backup-{int(time.time())}"
        primary_client.write_backup_blob(backup_name, raw_ciphertext)
        logger.warning("Backed up the undecryptable vault as '%s' before replacing it.", backup_name)
    except Exception:
        logger.warning("Could not back up the undecryptable vault -- proceeding without one.")


def save_vault(primary_client: DriveClient, vault: dict) -> None:
    primary_client.write_vault_raw(encrypt(json.dumps(vault)))


def add_or_update_account(vault: dict, google_sub: str, email: str, refresh_token: str, label: str) -> None:
    vault["accounts"][google_sub] = {
        "email": email,
        "refresh_token": encrypt(refresh_token),
        "label": label,
        "primary": False,
    }


def register_primary_account(vault: dict, google_sub: str, email: str) -> None:
    """
    The primary account's refresh token is never stored in the vault --
    it lives only in the signed session cookie. We still record a label
    entry here purely so the dashboard can list it alongside secondaries.
    """
    if google_sub not in vault["accounts"]:
        vault["accounts"][google_sub] = {
            "email": email,
            "refresh_token": None,
            "label": "Drive 1 (primary)",
            "primary": True,
        }


def decrypted_refresh_token(vault: dict, google_sub: str) -> str:
    return decrypt(vault["accounts"][google_sub]["refresh_token"])


def record_file(vault: dict, filename: str, size: int, chunks: list[dict]) -> None:
    vault["files"][filename] = {
        "size": size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "chunks": chunks,
    }


def remove_file(vault: dict, filename: str) -> dict | None:
    return vault["files"].pop(filename, None)


# ---------------------------------------------------------------------
# folders (virtual paths -- see module docstring)
# ---------------------------------------------------------------------
def normalize_path(raw: str) -> str:
    """Collapses slashes, strips '.'/'..'/empty segments -- the one gate
    every path from a client goes through before touching the vault."""
    if not raw:
        return ""
    segments = [s for s in raw.split("/") if s not in ("", ".", "..")]
    return "/".join(segments)


def is_valid_folder_name(name: str) -> bool:
    """A folder NAME is a single path segment, not a path itself."""
    return bool(name) and "/" not in name and name not in (".", "..")


def parent_path(path: str) -> str:
    parts = path.split("/")[:-1]
    return "/".join(parts)


def _ancestor_folders(path: str) -> list[str]:
    """All ancestor directory paths of a file/folder path, shallow to deep."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def all_folder_paths(vault: dict) -> set[str]:
    """Explicitly-created folders, plus any implied by existing file paths
    (so a folder always shows up even if it was never explicitly created)."""
    folders = set(vault.get("folders", []))
    for file_key in vault["files"]:
        folders.update(_ancestor_folders(file_key))
    folders.discard("")
    return folders


def list_folder_contents(vault: dict, folder_path: str) -> tuple[list[str], list[str]]:
    """Returns (direct subfolder paths, direct file keys) under folder_path."""
    folder_path = normalize_path(folder_path)
    folders = all_folder_paths(vault)
    subfolders = sorted(f for f in folders if parent_path(f) == folder_path and f != folder_path)
    files_here = sorted(k for k in vault["files"] if parent_path(k) == folder_path)
    return subfolders, files_here


def create_folder(vault: dict, parent: str, name: str) -> str:
    """Returns the new folder's full path."""
    parent = normalize_path(parent)
    full_path = f"{parent}/{name}" if parent else name
    if full_path not in vault.get("folders", []):
        vault.setdefault("folders", []).append(full_path)
    return full_path


def delete_folder_recursive(vault: dict, folder_path: str) -> list[dict]:
    """Removes a folder, every file under it, and every subfolder under it
    from the vault. Returns the list of Drive chunks the caller still needs
    to delete from the actual drives (vault itself has no Drive access)."""
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"

    files_to_remove = [k for k in vault["files"] if k == folder_path or k.startswith(prefix)]
    chunks: list[dict] = []
    for key in files_to_remove:
        chunks.extend(vault["files"][key]["chunks"])
        del vault["files"][key]

    vault["folders"] = [
        f for f in vault.get("folders", [])
        if not (f == folder_path or f.startswith(prefix))
    ]
    return chunks
