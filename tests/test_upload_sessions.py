"""
Tests the resumable-upload session tracker in isolation -- real files on
disk, real os.pread/pwrite, since this is the part responsible for not
losing (or corrupting) a large in-progress upload across a dropped
connection.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import shutil
import time

import pytest  # noqa: E402

from app import upload_sessions  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_session_root(tmp_path, monkeypatch):
    """Never touch the real OS temp dir from tests -- a fresh directory
    per test, cleaned up automatically by pytest's tmp_path."""
    monkeypatch.setattr(upload_sessions, "SESSION_ROOT", str(tmp_path / "sessions"))
    yield
    shutil.rmtree(str(tmp_path / "sessions"), ignore_errors=True)


def test_init_session_starts_fresh_at_zero():
    status = upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=25, chunk_size=10)
    assert status == {"received_chunks": 0, "total_chunks": 3}  # ceil(25/10)


def test_write_chunk_advances_received_count_in_order():
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=25, chunk_size=10)
    assert upload_sessions.write_chunk("user-1", "sess-a", 0, b"x" * 10) == 1
    assert upload_sessions.write_chunk("user-1", "sess-a", 1, b"y" * 10) == 2
    assert upload_sessions.write_chunk("user-1", "sess-a", 2, b"z" * 5) == 3


def test_write_chunk_is_idempotent_for_an_already_received_chunk():
    """A retry after the client didn't see the ack for a chunk it did
    successfully send must not corrupt anything or double-advance."""
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=20, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)
    # Client thinks chunk 0 might not have landed -- resends it.
    result = upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)
    assert result == 1  # unchanged, not double-counted


def test_write_chunk_rejects_out_of_order_chunk():
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=30, chunk_size=10)
    with pytest.raises(upload_sessions.SessionError):
        upload_sessions.write_chunk("user-1", "sess-a", 2, b"z" * 10)  # skipped chunk 0 and 1


def test_write_chunk_unknown_session_raises():
    with pytest.raises(upload_sessions.SessionError):
        upload_sessions.write_chunk("user-1", "no-such-session", 0, b"x")


def test_resuming_reports_exactly_what_was_already_received():
    """The actual feature: re-calling init_session for the same file
    (same id/name/size) after some chunks already landed -- e.g. the
    browser tab was closed and the same file re-selected later -- must
    report back how far it got, not restart from zero."""
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=30, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)
    upload_sessions.write_chunk("user-1", "sess-a", 1, b"b" * 10)

    # Simulates the connection dropping, then the client coming back and
    # re-initializing the exact same session.
    status = upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=30, chunk_size=10)
    assert status["received_chunks"] == 2
    assert status["total_chunks"] == 3

    # And can finish from there.
    upload_sessions.write_chunk("user-1", "sess-a", 2, b"c" * 10)
    fd, size, filename, folder = upload_sessions.finalize_session("user-1", "sess-a")
    try:
        assert size == 30
        assert filename == "movie.mp4"
        assert folder == "videos"
        assert os.pread(fd, 30, 0) == b"a" * 10 + b"b" * 10 + b"c" * 10
    finally:
        os.close(fd)


def test_reinit_with_different_file_metadata_starts_over_instead_of_reusing_stale_data():
    """A session id collision with an unrelated, different file (should
    never happen given how the id is derived client-side, but must not
    silently corrupt/misattribute data if it somehow does) starts clean
    rather than reporting bogus progress against the wrong file."""
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=30, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)

    status = upload_sessions.init_session("user-1", "sess-a", "different.mp4", "videos", total_size=999, chunk_size=10)
    assert status["received_chunks"] == 0


def test_finalize_before_all_chunks_received_raises():
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=30, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)
    with pytest.raises(upload_sessions.SessionError):
        upload_sessions.finalize_session("user-1", "sess-a")


def test_cleanup_session_removes_everything():
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=10, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)
    upload_sessions.cleanup_session("user-1", "sess-a")

    # A fresh init after cleanup must behave like a brand new session,
    # not resume the deleted one.
    status = upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=10, chunk_size=10)
    assert status["received_chunks"] == 0


def test_sessions_are_isolated_per_user():
    upload_sessions.init_session("user-1", "sess-a", "movie.mp4", "videos", total_size=10, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-a", 0, b"a" * 10)

    # A different user using the same session_id (plausible if the id is
    # derived only from filename/size/lastModified, which two different
    # people could easily share) must not see user-1's progress.
    status = upload_sessions.init_session("user-2", "sess-a", "movie.mp4", "videos", total_size=10, chunk_size=10)
    assert status["received_chunks"] == 0


def test_stale_abandoned_sessions_are_swept_on_next_init(monkeypatch):
    upload_sessions.init_session("user-1", "sess-old", "old.mp4", "", total_size=10, chunk_size=10)
    upload_sessions.write_chunk("user-1", "sess-old", 0, b"a" * 10)

    # Backdate its last_activity past the staleness window.
    session_dir = upload_sessions._session_dir("user-1", "sess-old")
    meta = upload_sessions._read_meta(session_dir)
    meta["last_activity"] = time.time() - upload_sessions.STALE_AFTER_SECONDS - 1
    import json
    with open(upload_sessions._meta_path(session_dir), "w") as fh:
        json.dump(meta, fh)

    # Triggered by any new init call for this user, not just the stale one.
    upload_sessions.init_session("user-1", "sess-new", "new.mp4", "", total_size=10, chunk_size=10)

    assert upload_sessions._read_meta(session_dir) is None


def test_rejects_unsafe_session_id():
    with pytest.raises(ValueError):
        upload_sessions.init_session("user-1", "../../etc", "x", "", total_size=10, chunk_size=10)


def test_rejects_unsafe_user_sub():
    with pytest.raises(ValueError):
        upload_sessions.init_session("../etc", "sess-a", "x", "", total_size=10, chunk_size=10)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
