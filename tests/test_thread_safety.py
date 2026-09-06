"""
Directly verifies the fix for the reported bug: multiple files landing on
the same drive must not share one DriveClient/service object across
threads. fresh_copy() must produce an independent underlying connection
object each time, and do so cheaply enough not to matter.
"""
import os
import threading

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app.drive_client import DriveClient  # noqa: E402
from app.workers import parallel_map  # noqa: E402


def test_fresh_copy_returns_independent_service_objects():
    client = DriveClient("fake-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "test-client-id", "test-client-secret")
    copy_a = client.fresh_copy()
    copy_b = client.fresh_copy()

    # Must not be the same object, and must not share the same underlying
    # httplib2 connection object -- that shared state was the root cause.
    assert copy_a is not client
    assert copy_b is not client
    assert copy_a._service is not client._service
    assert copy_a._service is not copy_b._service
    # http-level object identity is the actual thing that was getting
    # corrupted under concurrency -- confirm it truly differs too.
    assert copy_a._service._http is not client._service._http


def test_fresh_copy_is_safe_to_call_concurrently_from_many_threads():
    client = DriveClient("fake-refresh-token", ["https://www.googleapis.com/auth/drive.file"], "test-client-id", "test-client-secret")
    seen_services = []  # hold strong references so ids can't be GC'd and reused
    lock = threading.Lock()

    def worker(_):
        c = client.fresh_copy()
        with lock:
            seen_services.append(c._service)
        return True

    results = parallel_map(worker, list(range(20)))
    assert all(results)
    # Every concurrent caller got its own distinct service object -- no
    # two threads were ever sharing one connection.
    assert len({id(s) for s in seen_services}) == len(seen_services) == 20


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
