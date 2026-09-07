"""
Resumable upload sessions: a browser splits a file into fixed-size
chunks and uploads them one at a time; the server remembers how many
it's received (on disk, keyed by a client-computed deterministic id)
so a dropped connection -- or the user closing the tab entirely and
coming back later -- resumes from the last unacknowledged chunk instead
of restarting the whole transfer.

Deliberately simple rather than a general-purpose protocol: chunks for
one session must arrive strictly in order. That's enough to make a
power cut or a flaky connection cheap to recover from (the actual
reported problem) without needing a full range-bitmap/out-of-order
design. A session's "received_chunks" is just a count of how many
chunks, 0..N-1 contiguously from the start, are already written.

Session state lives under the OS temp directory, in the actual
container's own filesystem -- this survives the container process
restarting, but not a redeploy (which recreates the container). That
matches the failure mode this exists for (a dropped client connection,
not a server redeploy); nothing here claims to survive the latter.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

SESSION_ROOT = os.path.join(tempfile.gettempdir(), "drive-vault-upload-sessions")

# Anything untouched this long is almost certainly abandoned (user gave
# up, or the tab was closed and never reopened) -- swept on next access
# rather than needing a background job.
STALE_AFTER_SECONDS = 48 * 60 * 60


class SessionError(Exception):
    """Client is out of sync with the server's view of a session (e.g.
    sent a chunk index that isn't the next expected one). The client's
    fix is to call init_session() again and re-sync from there."""


def _user_root(user_sub: str) -> str:
    # Rejects a sub containing path separators outright rather than
    # trying to sanitize it -- Google's own account ids never look like
    # this, so it's only ever a sign something's gone wrong upstream.
    if "/" in user_sub or ".." in user_sub:
        raise ValueError(f"unsafe user_sub for session path: {user_sub!r}")
    return os.path.join(SESSION_ROOT, user_sub)


def _session_dir(user_sub: str, session_id: str) -> str:
    if "/" in session_id or ".." in session_id or not session_id:
        raise ValueError(f"unsafe session_id: {session_id!r}")
    return os.path.join(_user_root(user_sub), session_id)


def _meta_path(session_dir: str) -> str:
    return os.path.join(session_dir, "meta.json")


def _data_path(session_dir: str) -> str:
    return os.path.join(session_dir, "data.bin")


def _read_meta(session_dir: str) -> dict | None:
    try:
        with open(_meta_path(session_dir)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _write_meta(session_dir: str, meta: dict) -> None:
    meta["last_activity"] = time.time()
    tmp_path = _meta_path(session_dir) + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(meta, fh)
    os.replace(tmp_path, _meta_path(session_dir))  # atomic, so a crash mid-write can't corrupt it


def _sweep_stale_sessions(user_sub: str) -> None:
    """Best-effort cleanup of abandoned sessions, run opportunistically
    on init rather than on a schedule. Never lets a cleanup failure
    block the actual upload."""
    root = _user_root(user_sub)
    try:
        entries = os.listdir(root)
    except FileNotFoundError:
        return
    now = time.time()
    for entry in entries:
        session_dir = os.path.join(root, entry)
        meta = _read_meta(session_dir)
        if meta is None or now - meta.get("last_activity", 0) > STALE_AFTER_SECONDS:
            try:
                cleanup_session(user_sub, entry)
            except Exception:
                pass


def init_session(user_sub: str, session_id: str, filename: str, folder: str, total_size: int, chunk_size: int) -> dict:
    """Creates a new session, or -- if one already exists for this exact
    (session_id, filename, total_size) -- reports how far it already
    got, so the caller knows whether to resume or start fresh. Returns
    {"received_chunks": int, "total_chunks": int}."""
    _sweep_stale_sessions(user_sub)

    session_dir = _session_dir(user_sub, session_id)
    total_chunks = max(1, -(-total_size // chunk_size))  # ceil division

    existing = _read_meta(session_dir)
    if existing and existing["filename"] == filename and existing["total_size"] == total_size and existing["chunk_size"] == chunk_size:
        _write_meta(session_dir, existing)  # just bumps last_activity
        return {"received_chunks": existing["received_chunks"], "total_chunks": total_chunks}

    # Either genuinely new, or an id collision with a different file --
    # either way, start this session fresh.
    os.makedirs(session_dir, exist_ok=True)
    with open(_data_path(session_dir), "wb"):
        pass  # just needs to exist; chunks are written at their offsets via pwrite
    meta = {
        "filename": filename, "folder": folder, "total_size": total_size,
        "chunk_size": chunk_size, "received_chunks": 0,
    }
    _write_meta(session_dir, meta)
    return {"received_chunks": 0, "total_chunks": total_chunks}


def write_chunk(user_sub: str, session_id: str, chunk_index: int, data: bytes) -> int:
    """Returns the session's updated received_chunks count. Idempotent
    for a chunk that's already been received (a retry after the client
    didn't see the previous ack) -- just confirms it, doesn't re-write
    it. Raises SessionError if chunk_index is neither the next expected
    chunk nor already-received, since that means the client's view of
    this session has drifted out of sync somehow."""
    session_dir = _session_dir(user_sub, session_id)
    meta = _read_meta(session_dir)
    if meta is None:
        raise SessionError(f"no such session: {session_id}")

    if chunk_index < meta["received_chunks"]:
        return meta["received_chunks"]  # already have it -- harmless duplicate
    if chunk_index > meta["received_chunks"]:
        raise SessionError(f"expected chunk {meta['received_chunks']}, got {chunk_index}")

    offset = chunk_index * meta["chunk_size"]
    fd = os.open(_data_path(session_dir), os.O_WRONLY)
    try:
        os.pwrite(fd, data, offset)
    finally:
        os.close(fd)

    meta["received_chunks"] += 1
    _write_meta(session_dir, meta)
    return meta["received_chunks"]


def finalize_session(user_sub: str, session_id: str) -> tuple[int, int, str, str]:
    """Verifies every chunk arrived, then returns (fd, total_size,
    filename, folder) for the existing distributor.upload_many pipeline
    to read from directly -- the assembled file is handed off exactly
    the same way an UploadFile's own fd is. Caller is responsible for
    closing the fd and calling cleanup_session() once actually done
    with it (main.py does both after distribution finishes)."""
    session_dir = _session_dir(user_sub, session_id)
    meta = _read_meta(session_dir)
    if meta is None:
        raise SessionError(f"no such session: {session_id}")

    total_chunks = max(1, -(-meta["total_size"] // meta["chunk_size"]))
    if meta["received_chunks"] != total_chunks:
        raise SessionError(f"session incomplete: {meta['received_chunks']}/{total_chunks} chunks received")

    fd = os.open(_data_path(session_dir), os.O_RDONLY)
    return fd, meta["total_size"], meta["filename"], meta["folder"]


def cleanup_session(user_sub: str, session_id: str) -> None:
    session_dir = _session_dir(user_sub, session_id)
    data_path = _data_path(session_dir)
    meta_path = _meta_path(session_dir)
    for path in (data_path, meta_path, meta_path + ".tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(session_dir)
    except OSError:
        pass  # not empty or already gone -- fine either way
