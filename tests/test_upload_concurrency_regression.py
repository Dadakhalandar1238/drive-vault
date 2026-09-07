"""
Reproduces the reported bug directly: several small files all get routed
to the SAME drive (the roomiest one) and their uploads run concurrently.
Before the fix, upload_many() called .upload_bytes() on one shared
DriveClient per account; here we simulate what "shared" vs "fresh_copy"
means by having FakeDriveClient track whether any two calls ever
overlapped on the same instance.
"""
import os
import tempfile
import threading
import time

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app import distributor  # noqa: E402

GB = 1024 ** 3


class FakeDriveClient:
    """Stands in for DriveClient. Records concurrent-overlap on the SAME
    instance (the bug) vs always getting a fresh instance (the fix)."""

    def __init__(self, name, shared_state):
        self.name = name
        self._shared_state = shared_state  # {"in_flight": set(), "overlap_detected": bool}

    def fresh_copy(self):
        # Real DriveClient.fresh_copy() returns a brand-new instance --
        # mirror that here so upload_many exercises the real code path.
        return FakeDriveClient(self.name, self._shared_state)

    def upload_from_fd(self, name, fd, offset, length):
        my_id = id(self)
        state = self._shared_state
        with state["lock"]:
            if my_id in state["in_flight"]:
                state["overlap_detected"] = True
            state["in_flight"].add(my_id)
        time.sleep(0.05)  # simulate network latency, giving overlap a chance to happen
        with state["lock"]:
            state["in_flight"].discard(my_id)
        return f"fake-file-id-{name}"

    def get_free_space(self):
        return 100 * GB  # plenty of room on every "drive" for this test


def test_multiple_files_same_drive_do_not_share_a_connection():
    shared_state = {"lock": threading.Lock(), "in_flight": set(), "overlap_detected": False}
    # Three accounts, all with tons of free space -- every file below will
    # independently pick the same "roomiest" drive, exactly like the bug report.
    clients = {
        "drive-a": FakeDriveClient("drive-a", shared_state),
        "drive-b": FakeDriveClient("drive-b", shared_state),
        "drive-c": FakeDriveClient("drive-c", shared_state),
    }
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"x" * 1000)
    tmp.flush()
    fd = os.open(tmp.name, os.O_RDONLY)
    payloads = [(f"file{i}.jpg", fd, 1000) for i in range(8)]

    try:
        result = distributor.upload_many(clients, payloads)
    finally:
        os.close(fd)
        os.unlink(tmp.name)

    assert len(result) == 8
    for filename, chunks in result.items():
        assert len(chunks) == 1
        assert chunks[0]["file_id"].startswith("fake-file-id-")

    # This is the actual regression check: if upload_many still called
    # .upload_bytes() on a SHARED client instance instead of fresh_copy(),
    # concurrent uploads to the same drive would have overlapped on one
    # instance -- exactly the condition that corrupted the real httplib2
    # connection in the reported bug.
    assert shared_state["overlap_detected"] is False, (
        "Two concurrent uploads shared the same client instance -- "
        "this is the exact bug that caused the socket timeout / 500 error."
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
