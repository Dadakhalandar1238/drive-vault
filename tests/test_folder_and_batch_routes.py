"""
End-to-end route tests for: folder-aware upload, folder create/delete,
and single/batch file delete -- mocking only the Google-network boundary
(build_clients_and_vault / vault.save_vault), same seam as
test_dashboard_render.py.

Upload is exercised through the real resumable-session routes
(/upload/init, /upload/chunk, /upload/complete) rather than a single
POST, since that's how the app actually uploads now -- see
test_upload_sessions.py for the session-tracking logic itself, and
test_streaming_transfer.py for the fd-based transfer primitives.
"""
import itertools
import os
import shutil

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, session, upload_sessions, vault  # noqa: E402

GB = 1024 ** 3
_session_id_counter = itertools.count()


class FakeDriveClient:
    """Minimal stand-in: enough surface for distributor to call through."""

    def __init__(self, name="fake"):
        self.name = name
        self._next_id = 0

    def fresh_copy(self):
        return self

    def get_storage_info(self):
        return {"limit": 100 * GB, "usage": 0, "free": 100 * GB}

    def get_free_space(self):
        return 100 * GB

    def upload_from_fd(self, name, fd, offset, length):
        self._next_id += 1
        return f"fake-id-{self._next_id}"

    def download_to_fd(self, file_id, dest_fd, dest_offset=0):
        os.pwrite(dest_fd, b"fake-bytes", dest_offset)

    def delete_file(self, file_id):
        pass


def make_vault():
    return {
        "accounts": {"sub-primary": {"email": "me@example.com", "label": "Drive 1 (primary)", "primary": True}},
        "folders": [],
        "files": {},
    }


@pytest.fixture(autouse=True)
def isolated_upload_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_sessions, "SESSION_ROOT", str(tmp_path / "upload-sessions"))
    yield
    shutil.rmtree(str(tmp_path / "upload-sessions"), ignore_errors=True)


@pytest.fixture
def env(monkeypatch):
    """Shared mutable vault + fake client, wired into main.py's one seam."""
    v = make_vault()
    clients = {"sub-primary": FakeDriveClient()}

    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: (clients, v))
    monkeypatch.setattr(vault, "save_vault", lambda client, vv: None)

    client = TestClient(app=main.app)
    cookie = session.create_session_cookie("sub-primary", "me@example.com", "fake-refresh-token", "test-client-id", "test-client-secret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
    return client, v


def do_upload(client, folder, filename, content):
    """Drives the real init -> chunk -> complete flow, all in one chunk
    (real test content here is always small) -- a full round trip
    through the actual routes, not a shortcut around them."""
    sid = f"test-session-{next(_session_id_counter)}"
    resp = client.post("/upload/init", json={
        "session_id": sid, "filename": filename, "folder": folder,
        "total_size": len(content), "chunk_size": max(len(content), 1),
    })
    assert resp.status_code == 200, resp.text

    resp = client.post(
        "/upload/chunk",
        data={"session_id": sid, "chunk_index": "0"},
        files={"chunk": ("chunk", content, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post("/upload/complete", json={"session_id": sid})
    assert resp.status_code == 200, resp.text
    return resp


def test_upload_into_folder_builds_path_prefixed_key(env):
    client, v = env
    do_upload(client, "docs/receipts", "jan.pdf", b"hello world")
    assert "docs/receipts/jan.pdf" in v["files"]
    assert v["files"]["docs/receipts/jan.pdf"]["size"] == len(b"hello world")


def test_upload_without_folder_uses_bare_filename(env):
    client, v = env
    do_upload(client, "", "root.txt", b"x")
    assert "root.txt" in v["files"]


def test_create_folder_route(env):
    client, v = env
    resp = client.post("/folders/create", data={"parent": "", "name": "photos"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "/dashboard?folder=photos" in resp.headers["location"]
    assert "photos" in v["folders"]


def test_create_folder_rejects_invalid_name(env):
    client, _ = env
    resp = client.post("/folders/create", data={"parent": "", "name": "a/b"}, follow_redirects=False)
    assert resp.status_code == 400


def test_download_with_slash_in_path(env):
    client, v = env
    do_upload(client, "docs", "a.txt", b"content")
    resp = client.get("/download/docs/a.txt")
    assert resp.status_code == 200
    assert resp.content == b"fake-bytes"
    # Content-Length lets the browser show real download progress instead
    # of just a spinner.
    assert resp.headers["content-length"] == str(len(b"fake-bytes"))


def test_single_delete_with_slash_in_path(env):
    client, v = env
    do_upload(client, "docs", "a.txt", b"content")
    assert "docs/a.txt" in v["files"]
    resp = client.request("DELETE", "/files/docs/a.txt")
    assert resp.status_code == 200
    assert "docs/a.txt" not in v["files"]


def test_batch_delete_removes_multiple_files(env):
    client, v = env
    do_upload(client, "", "a.txt", b"1")
    do_upload(client, "", "b.txt", b"2")
    do_upload(client, "", "c.txt", b"3")
    assert set(v["files"].keys()) == {"a.txt", "b.txt", "c.txt"}

    resp = client.post("/files/batch-delete", json={"filenames": ["a.txt", "c.txt"]})
    assert resp.status_code == 200
    assert set(v["files"].keys()) == {"b.txt"}
    assert set(resp.json()["removed"]) == {"a.txt", "c.txt"}


def test_folder_delete_route_removes_nested_contents(env):
    client, v = env
    client.post("/folders/create", data={"parent": "", "name": "docs"}, follow_redirects=False)
    do_upload(client, "docs", "a.txt", b"1")
    assert "docs/a.txt" in v["files"]

    resp = client.post("/folders/delete", json={"path": "docs"})
    assert resp.status_code == 200
    assert "docs/a.txt" not in v["files"]
    assert "docs" not in v["folders"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
