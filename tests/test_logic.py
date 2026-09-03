"""
These exercise the pure logic (crypto, session cookies, distribution
planning) without touching the real Google Drive API, so they run
anywhere -- no network, no credentials.
"""
import os

os.environ.setdefault("GOOGLE_CLIENT_ID", "test")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test")
os.environ.setdefault("REDIRECT_URI", "http://localhost:8000/oauth/callback")
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

from app import crypto, distributor, session, vault  # noqa: E402


def test_crypto_roundtrip():
    secret = "1//0abcdEXAMPLEREFRESHTOKEN"
    enc = crypto.encrypt(secret)
    assert enc != secret
    assert crypto.decrypt(enc) == secret


def test_session_cookie_roundtrip():
    cookie = session.create_session_cookie("sub123", "me@example.com", "refresh-token-xyz")
    data = session.read_session_cookie(cookie)
    assert data["sub"] == "sub123"
    assert data["email"] == "me@example.com"
    assert data["refresh_token"] == "refresh-token-xyz"


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
    payloads = [("small.txt", b"x" * 100), ("big.txt", b"y" * 1500)]
    file_plans = {}
    for filename, data in payloads:
        file_plans[filename] = distributor.plan_chunks(free_space, len(data))
    # small.txt fits on one drive
    assert len(file_plans["small.txt"]) == 1
    # big.txt (1500) needs both drives since each only has <=1000 free after small.txt
    total_big = sum(sz for _, _, sz in file_plans["big.txt"])
    assert total_big == 1500


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
