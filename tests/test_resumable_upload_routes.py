"""
Route-level proof that resumable uploads actually resume -- this is the
direct regression test for the reported bug (a 2.99GB upload interrupted
partway had to restart from zero). Exercises the real /upload/init,
/upload/chunk, /upload/complete routes end to end, including simulating
a dropped connection mid-upload and confirming the next /upload/init
reports genuine partial progress rather than starting over.
"""
import itertools
import os
import shutil

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, session, upload_sessions, vault  # noqa: E402

GB = 1024 ** 3
_id_counter = itertools.count()


class RecordingFakeDriveClient:
    """Actually stores what it's given (read via pread, exactly like a
    real DriveClient would), so tests can verify the final file handed
    to Drive is byte-for-byte correct -- not just that some upload
    happened."""

    store = {}

    def __init__(self):
        self._next_id = 0

    def fresh_copy(self):
        return self

    def get_storage_info(self):
        return {"limit": 100 * GB, "usage": 0, "free": 100 * GB}

    def get_free_space(self):
        return 100 * GB

    def upload_from_fd(self, name, fd, offset, length):
        self._next_id += 1
        file_id = f"fake-id-{self._next_id}"
        RecordingFakeDriveClient.store[file_id] = os.pread(fd, length, offset)
        return file_id

    def delete_file(self, file_id):
        pass


class NoSpaceFakeDriveClient:
    def fresh_copy(self):
        return self

    def get_free_space(self):
        return 0  # nothing fits, ever


@pytest.fixture(autouse=True)
def isolated_upload_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_sessions, "SESSION_ROOT", str(tmp_path / "upload-sessions"))
    yield
    shutil.rmtree(str(tmp_path / "upload-sessions"), ignore_errors=True)


@pytest.fixture
def env(monkeypatch):
    v = {"accounts": {"sub-primary": {"email": "me@example.com", "label": "Drive 1 (primary)", "primary": True}}, "folders": [], "files": {}}
    clients = {"sub-primary": RecordingFakeDriveClient()}
    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: (clients, v))
    monkeypatch.setattr(vault, "save_vault", lambda client, vv: None)

    client = TestClient(app=main.app)
    cookie = session.create_session_cookie("sub-primary", "me@example.com", "fake-refresh-token", "test-client-id", "test-client-secret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
    return client, v


def new_session_id():
    return f"sess-{next(_id_counter)}"


def test_upload_config_reports_the_real_chunk_size(env):
    client, _ = env
    from app.drive_client import TRANSFER_CHUNK_SIZE
    resp = client.get("/upload/config")
    assert resp.status_code == 200
    assert resp.json() == {"chunk_size": TRANSFER_CHUNK_SIZE}


def test_full_multi_chunk_upload_reassembles_correctly(env):
    client, v = env
    sid = new_session_id()
    content = bytes(range(256)) * 10  # 2560 distinct-ish bytes
    chunk_size = 1000  # -> 3 chunks: 1000, 1000, 560

    resp = client.post("/upload/init", json={
        "session_id": sid, "filename": "big.bin", "folder": "", "total_size": len(content), "chunk_size": chunk_size,
    })
    assert resp.status_code == 200
    assert resp.json() == {"received_chunks": 0, "total_chunks": 3}

    for i in range(3):
        piece = content[i * chunk_size:(i + 1) * chunk_size]
        resp = client.post(
            "/upload/chunk",
            data={"session_id": sid, "chunk_index": str(i)},
            files={"chunk": ("chunk", piece, "application/octet-stream")},
        )
        assert resp.status_code == 200
        assert resp.json()["received_chunks"] == i + 1

    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "key": "big.bin", "folder": ""}

    assert v["files"]["big.bin"]["size"] == len(content)
    file_id = v["files"]["big.bin"]["chunks"][0]["file_id"]
    assert RecordingFakeDriveClient.store[file_id] == content


def test_dropped_connection_resumes_instead_of_restarting(env):
    """The actual bug report: upload gets partway, connection drops, the
    client comes back and must resume from where it left off."""
    client, v = env
    sid = new_session_id()
    content = os.urandom(3000)
    chunk_size = 1000  # 3 chunks

    client.post("/upload/init", json={
        "session_id": sid, "filename": "movie.mp4", "folder": "", "total_size": len(content), "chunk_size": chunk_size,
    })
    # First chunk succeeds...
    client.post(
        "/upload/chunk",
        data={"session_id": sid, "chunk_index": "0"},
        files={"chunk": ("chunk", content[0:1000], "application/octet-stream")},
    )
    # ...then the connection drops before chunk 1 is acknowledged. The
    # client, having lost track of its in-memory state, does what the
    # real JS does on reconnect: calls /upload/init again.
    resume_status = client.post("/upload/init", json={
        "session_id": sid, "filename": "movie.mp4", "folder": "", "total_size": len(content), "chunk_size": chunk_size,
    }).json()
    assert resume_status["received_chunks"] == 1  # NOT 0 -- this is the whole point
    assert resume_status["total_chunks"] == 3

    # Client resumes from chunk 1, not chunk 0.
    for i in range(resume_status["received_chunks"], resume_status["total_chunks"]):
        piece = content[i * chunk_size:(i + 1) * chunk_size]
        resp = client.post(
            "/upload/chunk",
            data={"session_id": sid, "chunk_index": str(i)},
            files={"chunk": ("chunk", piece, "application/octet-stream")},
        )
        assert resp.status_code == 200

    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 200

    file_id = v["files"]["movie.mp4"]["chunks"][0]["file_id"]
    # Byte-for-byte correct despite the resume -- no duplication, no gap.
    assert RecordingFakeDriveClient.store[file_id] == content


def test_reselecting_the_same_file_after_closing_the_tab_also_resumes(env):
    """Same scenario, but simulating the browser tab having been closed
    entirely (no client-side state survives) and the user re-dragging
    the identical file back in later -- the session id is deterministic
    from the file's own metadata, so this looks identical to the server
    as the mid-upload-reconnect case above."""
    client, v = env
    # A real browser would compute this from name|size|lastModified; the
    # exact algorithm doesn't matter here, only that it's the same string
    # both times, which is what "the same file, re-selected" guarantees.
    sid = "deterministic-id-for-movie.mp4-3000-1700000000000"
    content = os.urandom(3000)
    chunk_size = 1000

    client.post("/upload/init", json={"session_id": sid, "filename": "movie.mp4", "folder": "", "total_size": len(content), "chunk_size": chunk_size})
    client.post("/upload/chunk", data={"session_id": sid, "chunk_index": "0"}, files={"chunk": ("c", content[:1000], "application/octet-stream")})
    client.post("/upload/chunk", data={"session_id": sid, "chunk_index": "1"}, files={"chunk": ("c", content[1000:2000], "application/octet-stream")})

    # "Tab closed" -- nothing more happens for a while, then the exact
    # same file is selected again, computing the exact same session id.
    status = client.post("/upload/init", json={"session_id": sid, "filename": "movie.mp4", "folder": "", "total_size": len(content), "chunk_size": chunk_size}).json()
    assert status["received_chunks"] == 2

    client.post("/upload/chunk", data={"session_id": sid, "chunk_index": "2"}, files={"chunk": ("c", content[2000:3000], "application/octet-stream")})
    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 200
    file_id = v["files"]["movie.mp4"]["chunks"][0]["file_id"]
    assert RecordingFakeDriveClient.store[file_id] == content


def test_upload_chunk_rejects_out_of_order_index(env):
    client, _ = env
    sid = new_session_id()
    client.post("/upload/init", json={"session_id": sid, "filename": "f.bin", "folder": "", "total_size": 30, "chunk_size": 10})
    resp = client.post(
        "/upload/chunk",
        data={"session_id": sid, "chunk_index": "2"},  # skipped 0 and 1
        files={"chunk": ("c", b"x" * 10, "application/octet-stream")},
    )
    assert resp.status_code == 409


def test_upload_complete_before_all_chunks_received_returns_409(env):
    client, _ = env
    sid = new_session_id()
    client.post("/upload/init", json={"session_id": sid, "filename": "f.bin", "folder": "", "total_size": 30, "chunk_size": 10})
    client.post("/upload/chunk", data={"session_id": sid, "chunk_index": "0"}, files={"chunk": ("c", b"x" * 10, "application/octet-stream")})
    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 409


def test_upload_complete_preserves_session_when_no_drive_has_room(monkeypatch):
    """If distribution fails (no space anywhere), the assembled upload
    must NOT be thrown away -- otherwise freeing up space and retrying
    would mean re-sending the entire file's bytes for nothing."""
    v = {"accounts": {"sub-primary": {"email": "me@example.com", "label": "Drive 1 (primary)", "primary": True}}, "folders": [], "files": {}}
    clients = {"sub-primary": NoSpaceFakeDriveClient()}
    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: (clients, v))
    monkeypatch.setattr(vault, "save_vault", lambda client, vv: None)

    client = TestClient(app=main.app)
    cookie = session.create_session_cookie("sub-primary", "me@example.com", "fake-refresh-token", "test-client-id", "test-client-secret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)

    sid = new_session_id()
    client.post("/upload/init", json={"session_id": sid, "filename": "f.bin", "folder": "", "total_size": 10, "chunk_size": 10})
    client.post("/upload/chunk", data={"session_id": sid, "chunk_index": "0"}, files={"chunk": ("c", b"x" * 10, "application/octet-stream")})

    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 400

    # The session must still report itself as fully received -- not reset.
    status = client.post("/upload/init", json={"session_id": sid, "filename": "f.bin", "folder": "", "total_size": 10, "chunk_size": 10}).json()
    assert status["received_chunks"] == 1
    assert status["total_chunks"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
