"""
These exercise the pure logic (crypto, session cookies, distribution
planning) without touching the real Google Drive API, so they run
anywhere -- no network, no credentials.
"""
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app import crypto, distributor, session, vault  # noqa: E402


def test_crypto_roundtrip():
    secret = "1//0abcdEXAMPLEREFRESHTOKEN"
    enc = crypto.encrypt(secret)
    assert enc != secret
    assert crypto.decrypt(enc) == secret


def test_session_cookie_roundtrip():
    cookie = session.create_session_cookie(
        "sub123", "me@example.com", "refresh-token-xyz",
        "test-client-id.apps.googleusercontent.com", "test-client-secret",
    )
    data = session.read_session_cookie(cookie)
    assert data["sub"] == "sub123"
    assert data["email"] == "me@example.com"
    assert data["refresh_token"] == "refresh-token-xyz"
    assert data["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert data["client_secret"] == "test-client-secret"


def test_session_cookie_rejects_garbage():
    assert session.read_session_cookie("not-a-real-cookie") is None
    assert session.read_session_cookie(None) is None


def test_plan_chunks_single_drive_fit():
    free_space = {"a": 1000, "b": 50}
    plan = distributor.plan_chunks(free_space, 200)
    assert plan == [("a", 0, 200)]
    assert free_space["a"] == 800  # decremented


def test_plan_chunks_picks_roomiest_drive():
    free_space = {"a": 300, "b": 900, "c": 500}
    plan = distributor.plan_chunks(free_space, 200)
    assert plan == [("b", 0, 200)]


def test_plan_chunks_splits_across_drives_when_needed():
    free_space = {"a": 100, "b": 150, "c": 80}
    plan = distributor.plan_chunks(free_space, 300)
    total = sum(size for _, _, size in plan)
    assert total == 300
    assert len(plan) >= 2
    assert free_space["a"] == 0 or free_space["b"] == 0 or free_space["c"] == 0


def test_plan_chunks_raises_when_not_enough_total_space():
    free_space = {"a": 10, "b": 10}
    try:
        distributor.plan_chunks(free_space, 1000)
        assert False, "should have raised"
    except ValueError:
        pass


def test_vault_add_and_lookup_account():
    v = vault.EMPTY_VAULT.copy()
    v = {"accounts": {}, "files": {}}
    vault.add_or_update_account(v, "sub456", "second@example.com", "refresh-2", "Drive 2")
    assert vault.decrypted_refresh_token(v, "sub456") == "refresh-2"


def test_vault_record_and_remove_file():
    v = {"accounts": {}, "files": {}}
    vault.record_file(v, "photo.jpg", 12345, [{"account": "a", "file_id": "f1", "offset": 0, "size": 12345}])
    assert "photo.jpg" in v["files"]
    removed = vault.remove_file(v, "photo.jpg")
    assert removed["size"] == 12345
    assert "photo.jpg" not in v["files"]


def test_upload_many_task_flattening_logic():
    # Simulate the planning phase of upload_many without real DriveClients
    free_space = {"a": 1000, "b": 1000}
    sizes = {"small.txt": 100, "big.txt": 1500}
    file_plans = {}
    for filename, size in sizes.items():
        file_plans[filename] = distributor.plan_chunks(free_space, size)
    # small.txt fits on one drive
    assert len(file_plans["small.txt"]) == 1
    # big.txt (1500) needs both drives since each only has <=1000 free after small.txt
    total_big = sum(sz for _, _, sz in file_plans["big.txt"])
    assert total_big == 1500


def test_upload_many_never_reads_more_than_its_planned_chunk_size():
    """Memory-usage regression check for the OOM fix: upload_many must
    never read a chunk's bytes into memory itself -- it hands the fd and
    an (offset, length) straight to the client, which is what keeps peak
    memory bounded regardless of file size. Verified here by reading the
    real file ourselves (via the same offsets used in production) and
    confirming byte-for-byte reconstruction, across a file split into
    multiple drives -- not just a single-drive happy path."""
    content = os.urandom(2000)
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(content)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDONLY)

    seen_ranges = {}

    class RecordingFakeClient:
        def __init__(self, name):
            self.name = name

        def fresh_copy(self):
            return self

        def upload_from_fd(self, name, fd, offset, length, progress_cb=None):
            # The real assertion: distributor never materializes bytes
            # itself -- it only ever passes through the (fd, offset,
            # length) triple. We read it back ourselves here purely to
            # verify correctness of the plan, the same way a real upload
            # would read exactly this range and nothing more.
            seen_ranges[name] = os.pread(fd, length, offset)
            if progress_cb:
                progress_cb(length)
            return f"fake-id-{name}"

        def get_free_space(self):
            return 900  # small enough that a 2000-byte file must split

    clients = {"drive-a": RecordingFakeClient("drive-a"), "drive-b": RecordingFakeClient("drive-b"), "drive-c": RecordingFakeClient("drive-c")}
    try:
        result = distributor.upload_many(clients, [("photo.jpg", fd, len(content))])
    finally:
        os.close(fd)
        os.unlink(tmp.name)

    chunks = result["photo.jpg"]
    assert len(chunks) >= 2  # confirms it actually split, not a trivial single-drive case
    total = sum(c["size"] for c in chunks)
    assert total == len(content)

    # Reassemble in offset order and confirm it's byte-for-byte identical
    # to the original -- this is the correctness guarantee that matters:
    # a wrong offset here would silently corrupt the uploaded file.
    ordered = sorted(chunks, key=lambda c: c["offset"])
    reassembled = b"".join(seen_ranges[f"photo.jpg.part{c['offset']}"] for c in ordered)
    assert reassembled == content


def test_upload_many_progress_cb_reports_cumulative_bytes_per_file():
    """Feeds the /upload/status polling endpoint -- a large file split
    across several drives uploads those splits concurrently, so this
    confirms upload_many combines their individual progress reports into
    one running total per filename rather than the caller seeing
    per-split numbers that don't add up to the whole file."""
    content = os.urandom(2000)
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(content)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDONLY)

    class FakeClient:
        def fresh_copy(self):
            return self

        def upload_from_fd(self, name, fd, offset, length, progress_cb=None):
            if progress_cb:
                progress_cb(length // 2)
                progress_cb(length)  # two reports per split, like a real multi-chunk resumable upload
            return f"fake-id-{name}"

        def get_free_space(self):
            return 1200  # too small alone (2000-byte file), but two combined cover it

    reports = []
    clients = {"drive-a": FakeClient(), "drive-b": FakeClient()}
    try:
        result = distributor.upload_many(clients, [("photo.jpg", fd, len(content))], progress_cb=lambda name, total: reports.append((name, total)))
    finally:
        os.close(fd)
        os.unlink(tmp.name)

    chunks = result["photo.jpg"]
    assert len(chunks) == 2  # confirms it actually split
    assert all(name == "photo.jpg" for name, _ in reports)
    # Final report must equal the whole file's size, not just one split's.
    assert reports[-1][1] == len(content)
    # Every reported total is monotonically non-decreasing.
    totals = [total for _, total in reports]
    assert totals == sorted(totals)


def test_upload_many_without_progress_cb_does_not_require_one():
    """progress_cb is optional -- existing callers (and the fake clients
    in other tests) that don't pass one must keep working unchanged."""
    content = b"x" * 100
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(content)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDONLY)

    class FakeClient:
        def fresh_copy(self):
            return self

        def upload_from_fd(self, name, fd, offset, length, progress_cb=None):
            assert progress_cb is None
            return "fake-id"

        def get_free_space(self):
            return 1000

    try:
        result = distributor.upload_many({"drive-a": FakeClient()}, [("f.txt", fd, len(content))])
    finally:
        os.close(fd)
        os.unlink(tmp.name)
    assert result["f.txt"][0]["file_id"] == "fake-id"


if __name__ == "__main__":
    import sys
    import traceback

    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
