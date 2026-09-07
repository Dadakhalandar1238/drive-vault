"""
Tests the streaming upload/download primitives that replaced the old
"read the whole file into a bytes object" approach -- the fix for the
20GB-file OOM risk. These need real byte-level verification, not just
mock-call assertions: an off-by-one here would silently corrupt files.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "unit-test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "unit-test-client-secret")

import tempfile
from unittest.mock import MagicMock

import pytest  # noqa: E402

from app.drive_client import (  # noqa: E402
    SIMPLE_UPLOAD_THRESHOLD,
    TRANSFER_CHUNK_SIZE,
    DriveClient,
    _FdOffsetWriter,
    _FdRangeReader,
)


@pytest.fixture
def tmp_content():
    """A real file on disk with known, non-repeating content, so a wrong
    offset/length is detectable rather than accidentally matching."""
    content = bytes(range(256)) * 200  # 51200 distinct-ish bytes
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(content)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDONLY)
    yield fd, content, tmp.name
    os.close(fd)
    os.unlink(tmp.name)


# ---------------------------------------------------------------------
# _FdRangeReader -- the upload side
# ---------------------------------------------------------------------
def test_fd_range_reader_reads_exact_byte_range(tmp_content):
    fd, content, _ = tmp_content
    reader = _FdRangeReader(fd, offset=100, length=50)
    assert reader.read() == content[100:150]


def test_fd_range_reader_respects_bounded_read_size(tmp_content):
    """The whole point: a caller asking for more than TRANSFER_CHUNK_SIZE
    at once is exactly the pattern MediaIoBaseUpload uses to stream a
    large file in bounded pieces -- reading past the reader's own
    declared length must never happen, even if asked for more."""
    fd, content, _ = tmp_content
    reader = _FdRangeReader(fd, offset=10, length=20)
    first = reader.read(1000)  # ask for way more than the range holds
    assert first == content[10:30]
    assert reader.read(1000) == b""  # exhausted


def test_fd_range_reader_incremental_reads_stay_in_order(tmp_content):
    fd, content, _ = tmp_content
    reader = _FdRangeReader(fd, offset=0, length=100)
    collected = b""
    while True:
        piece = reader.read(7)  # deliberately awkward chunk size
        if not piece:
            break
        collected += piece
    assert collected == content[0:100]


def test_fd_range_reader_seek_and_tell(tmp_content):
    fd, content, _ = tmp_content
    reader = _FdRangeReader(fd, offset=200, length=100)
    reader.seek(50)
    assert reader.tell() == 50
    assert reader.read(10) == content[250:260]


# ---------------------------------------------------------------------
# _FdOffsetWriter -- the download side
# ---------------------------------------------------------------------
def test_fd_offset_writer_writes_at_the_correct_absolute_position():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"\x00" * 1000)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDWR)
    try:
        writer = _FdOffsetWriter(fd, base_offset=300)
        writer.write(b"HELLO")
        writer.seek(10)  # MediaIoBaseDownload reports absolute progress via seek
        writer.write(b"WORLD")

        result = os.pread(fd, 1000, 0)
        assert result[300:305] == b"HELLO"
        assert result[310:315] == b"WORLD"
    finally:
        os.close(fd)
        os.unlink(tmp.name)


def test_multiple_offset_writers_share_one_fd_without_colliding():
    """Simulates two concurrent chunk downloads for the same file,
    landing at different offsets in the same destination file."""
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"\x00" * 20)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDWR)
    try:
        writer_a = _FdOffsetWriter(fd, base_offset=0)
        writer_b = _FdOffsetWriter(fd, base_offset=10)
        writer_a.write(b"AAAAA")
        writer_b.write(b"BBBBB")
        writer_a.write(b"AAAAA")
        writer_b.write(b"BBBBB")

        result = os.pread(fd, 20, 0)
        assert result == b"AAAAAAAAAA" + b"BBBBBBBBBB"
    finally:
        os.close(fd)
        os.unlink(tmp.name)


# ---------------------------------------------------------------------
# DriveClient.upload_from_fd -- small vs. large routing
# ---------------------------------------------------------------------
def _client_with_mocked_service():
    client = DriveClient("fake-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "cid", "csecret")
    client._service = MagicMock()
    return client


# ---------------------------------------------------------------------
# Real production bug (2.5GB upload): httplib2 auto-follows HTTP 308 as
# a redirect by default, but Google's resumable-upload protocol reuses
# 308 to mean "chunk received, send the next one" -- with no Location
# header, since it isn't really a redirect. httplib2 crashed with
# RedirectMissingLocation the moment a multi-chunk upload got past its
# first chunk. Only surfaces for files over TRANSFER_CHUNK_SIZE, which
# is exactly why nothing caught it earlier.
# ---------------------------------------------------------------------
def test_drive_client_excludes_308_from_httplib2_auto_redirects():
    client = DriveClient("fake-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "cid", "csecret")
    underlying_http = client._service._http.http
    assert 308 not in underlying_http.redirect_codes


def test_httplib2_would_crash_on_a_real_resumable_upload_continue_response_without_the_fix():
    """Reproduces the exact crash directly against httplib2's own
    redirect-following logic, independent of any Google/DriveClient
    mocking -- a real local server returning what Google's resumable
    upload API actually sends for "keep going" (308, no Location)."""
    import http.server
    import threading

    import httplib2

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            self.send_response(308, "Resume Incomplete")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        broken = httplib2.Http()
        with pytest.raises(httplib2.error.RedirectMissingLocation):
            broken.request(f"http://127.0.0.1:{port}/", method="PUT", body=b"x")

        fixed = httplib2.Http()
        fixed.redirect_codes = fixed.redirect_codes - {308}
        resp, _ = fixed.request(f"http://127.0.0.1:{port}/", method="PUT", body=b"x")
        assert resp.status == 308
    finally:
        server.shutdown()


def test_upload_from_fd_small_chunk_uses_simple_non_resumable_upload(tmp_content, monkeypatch):
    fd, content, _ = tmp_content
    client = _client_with_mocked_service()
    client._service.files.return_value.create.return_value.execute.return_value = {"id": "abc123"}

    captured = {}
    import app.drive_client as drive_client_module

    real_media_upload = drive_client_module.MediaIoBaseUpload

    def spy(fileobj, mimetype, resumable=False, chunksize=None):
        captured["resumable"] = resumable
        captured["data"] = fileobj.read()
        return real_media_upload(fileobj, mimetype=mimetype, resumable=resumable)

    monkeypatch.setattr(drive_client_module, "MediaIoBaseUpload", lambda fileobj, mimetype, resumable=False, chunksize=None: spy(fileobj, mimetype, resumable, chunksize))

    file_id = client.upload_from_fd("small.txt", fd, offset=5, length=30)

    assert file_id == "abc123"
    assert captured["resumable"] is False
    assert captured["data"] == content[5:35]


def test_upload_from_fd_large_chunk_uses_resumable_upload_with_bounded_chunksize(tmp_content, monkeypatch):
    fd, content, _ = tmp_content
    client = _client_with_mocked_service()

    fake_request = MagicMock()
    fake_request.next_chunk.return_value = (None, {"id": "big-file-id"})
    client._service.files.return_value.create.return_value = fake_request

    captured = {}
    import app.drive_client as drive_client_module

    def fake_media_upload(fileobj, mimetype, resumable=False, chunksize=None):
        captured["resumable"] = resumable
        captured["chunksize"] = chunksize
        captured["fileobj"] = fileobj
        return MagicMock()

    monkeypatch.setattr(drive_client_module, "MediaIoBaseUpload", fake_media_upload)

    large_length = SIMPLE_UPLOAD_THRESHOLD + 1
    file_id = client.upload_from_fd("big.bin", fd, offset=0, length=large_length)

    assert file_id == "big-file-id"
    assert captured["resumable"] is True
    # The whole point: never a bigger chunk than this, regardless of the
    # actual file size passed in.
    assert captured["chunksize"] == TRANSFER_CHUNK_SIZE
    assert isinstance(captured["fileobj"], drive_client_module._FdRangeReader)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
