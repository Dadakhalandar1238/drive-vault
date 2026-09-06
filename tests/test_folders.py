"""Tests for virtual folder support: path normalization/safety, listing,
creation, and recursive deletion."""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "unit-test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "unit-test-client-secret")

from app import vault  # noqa: E402


def test_normalize_path_strips_traversal_and_empty_segments():
    # ".." segments are dropped entirely (not resolved) -- simpler and can
    # never result in an escape above the vault's own namespace.
    assert vault.normalize_path("docs/../../etc/passwd") == "docs/etc/passwd"
    assert vault.normalize_path("//docs//photos//") == "docs/photos"
    assert vault.normalize_path("") == ""
    assert vault.normalize_path("./docs/./photos") == "docs/photos"


def test_is_valid_folder_name():
    assert vault.is_valid_folder_name("photos") is True
    assert vault.is_valid_folder_name("a/b") is False
    assert vault.is_valid_folder_name("") is False
    assert vault.is_valid_folder_name("..") is False
    assert vault.is_valid_folder_name(".") is False


def test_parent_path():
    assert vault.parent_path("docs/photos/a.jpg") == "docs/photos"
    assert vault.parent_path("a.jpg") == ""
    assert vault.parent_path("docs/a.jpg") == "docs"


def test_create_and_list_folder_contents():
    v = {"accounts": {}, "folders": [], "files": {}}
    root_docs = vault.create_folder(v, "", "docs")
    assert root_docs == "docs"
    nested = vault.create_folder(v, "docs", "receipts")
    assert nested == "docs/receipts"

    vault.record_file(v, "docs/report.pdf", 100, [])
    vault.record_file(v, "docs/receipts/jan.pdf", 50, [])
    vault.record_file(v, "root.txt", 10, [])

    root_subfolders, root_files = vault.list_folder_contents(v, "")
    assert root_subfolders == ["docs"]
    assert root_files == ["root.txt"]

    docs_subfolders, docs_files = vault.list_folder_contents(v, "docs")
    assert docs_subfolders == ["docs/receipts"]
    assert docs_files == ["docs/report.pdf"]

    receipts_subfolders, receipts_files = vault.list_folder_contents(v, "docs/receipts")
    assert receipts_subfolders == []
    assert receipts_files == ["docs/receipts/jan.pdf"]


def test_folder_is_implied_by_file_path_even_if_never_explicitly_created():
    v = {"accounts": {}, "folders": [], "files": {}}
    vault.record_file(v, "photos/vacation/beach.jpg", 100, [])
    subfolders, _ = vault.list_folder_contents(v, "")
    assert subfolders == ["photos"]
    subfolders2, _ = vault.list_folder_contents(v, "photos")
    assert subfolders2 == ["photos/vacation"]


def test_delete_folder_recursive_removes_files_subfolders_and_returns_chunks():
    v = {"accounts": {}, "folders": [], "files": {}}
    vault.create_folder(v, "", "docs")
    vault.create_folder(v, "docs", "receipts")
    chunk_a = [{"account": "acct1", "file_id": "f1", "offset": 0, "size": 10}]
    chunk_b = [{"account": "acct2", "file_id": "f2", "offset": 0, "size": 20}]
    vault.record_file(v, "docs/report.pdf", 10, chunk_a)
    vault.record_file(v, "docs/receipts/jan.pdf", 20, chunk_b)
    vault.record_file(v, "unrelated.txt", 5, [])

    removed_chunks = vault.delete_folder_recursive(v, "docs")

    assert {c["file_id"] for c in removed_chunks} == {"f1", "f2"}
    assert "docs/report.pdf" not in v["files"]
    assert "docs/receipts/jan.pdf" not in v["files"]
    assert "unrelated.txt" in v["files"]  # untouched
    assert "docs" not in v["folders"]
    assert "docs/receipts" not in v["folders"]


def test_load_vault_migrates_old_vault_missing_folders_key():
    # Simulates a vault saved before this feature existed.
    import json
    from unittest.mock import MagicMock
    from app.crypto import encrypt

    old_vault_json = json.dumps({"accounts": {}, "files": {"a.txt": {"size": 1, "uploaded_at": "x", "chunks": []}}})
    fake_client = MagicMock()
    fake_client.read_vault_raw.return_value = encrypt(old_vault_json)

    loaded = vault.load_vault(fake_client)
    assert loaded["folders"] == []
    assert "a.txt" in loaded["files"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
