"""
Exercises the real FastAPI app + Jinja2 templates end-to-end. Mocks at
main.build_clients_and_vault -- the one seam between "talk to Google" and
"render the page" -- so this proves the pool/tank math and template render
correctly without touching the network, using pytest's monkeypatch fixture.
"""
import os

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, session  # noqa: E402

GB = 1024 ** 3


class FakeDriveClient:
    def __init__(self, limit, usage):
        self.limit = limit
        self.usage = usage

    def get_storage_info(self):
        if self.limit is None:
            return {"limit": None, "usage": self.usage, "free": 10**15}
        return {"limit": self.limit, "usage": self.usage, "free": max(self.limit - self.usage, 0)}


# Mirrors the screenshot: primary (5112.36GB free of 5120), a 15GB drive with
# nothing on it, and a 15GB drive that's nearly full (1.97GB free -> "critical").
FAKE_CLIENTS = {
    "sub-primary": FakeDriveClient(limit=5120 * GB, usage=int(7.64 * GB)),
    "sub-secondary-1": FakeDriveClient(limit=15 * GB, usage=0),
    "sub-secondary-2": FakeDriveClient(limit=15 * GB, usage=int(13.03 * GB)),
}
FAKE_VAULT = {
    "accounts": {
        "sub-primary": {"email": "me@example.com", "label": "Drive 1 (primary)", "primary": True},
        "sub-secondary-1": {"email": "second@example.com", "label": "Drive 2", "primary": False},
        "sub-secondary-2": {"email": "third@example.com", "label": "Drive 3", "primary": False},
    },
    "files": {
        "husky2.jpg": {
            "size": 25165,
            "uploaded_at": "2026-09-03T15:41:09+00:00",
            "chunks": [{"account": "sub-primary", "file_id": "f1", "offset": 0, "size": 25165}],
        },
    },
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "build_clients_and_vault", lambda sess: (FAKE_CLIENTS, FAKE_VAULT))
    return TestClient(app=main.app)


def test_dashboard_renders_instantly_without_waiting_on_quota_checks(client):
    """The dashboard route itself must NOT call get_storage_overview --
    that's the whole point of the perf fix. We prove it by making
    get_storage_overview raise if called, and confirming /dashboard still
    renders fine (with skeleton placeholders) without hitting it."""
    import app.distributor as distributor_module

    def boom(_clients):
        raise AssertionError("dashboard route must not call get_storage_overview directly")

    original = distributor_module.get_storage_overview
    distributor_module.get_storage_overview = boom
    try:
        cookie = session.create_session_cookie("sub-primary", "me@example.com", "fake-refresh-token", "test-client-id", "test-client-secret")
        client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
        resp = client.get("/dashboard")
        assert resp.status_code == 200, resp.text
        html = resp.text
    finally:
        distributor_module.get_storage_overview = original

    # Per-drive tank shells render immediately from vault data alone
    for needle in ["Drive 1 (primary)", "Drive 2", "Drive 3", "second@example.com", "third@example.com"]:
        assert needle in html

    # Skeleton placeholders present, real numbers are NOT computed inline
    assert 'skeleton-fill' in html
    assert "5150.0" not in html  # would only appear if quota math ran synchronously

    # JS wiring to fetch the real numbers asynchronously is present
    assert "/api/storage-overview" in html
    assert "loadStorageOverview" in html

    # Delete/download/create-folder actions now go through JS with loader
    # feedback, not plain links/forms with no in-flight indication.
    assert "onclick=\"downloadFile(" in html
    assert "onclick=\"deleteFile(" in html
    assert "onclick=\"deleteFolder(" in html or "deleteFolder" in html
    assert "setButtonLoading" in html
    assert "newFolderForm.addEventListener('submit'" in html
    assert '<a class="btn secondary" href="/download/' not in html  # old plain-link download is gone

    # File manifest and upload/progress markup still present
    assert "husky2.jpg" in html
    assert 'id="dropzone"' in html
    assert 'id="progressFill"' in html
    assert "xhr.upload.onprogress" in html
    assert '/static/style.css' in html


def test_storage_overview_api_returns_correct_pool_and_tank_math(client):
    cookie = session.create_session_cookie("sub-primary", "me@example.com", "fake-refresh-token", "test-client-id", "test-client-secret")
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)

    resp = client.get("/api/storage-overview")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Pool totals: capacity 5120+15+15=5150GB, free ~5112.36+15+1.97=5129.33GB
    assert data["pool"]["total_capacity_gb"] == 5150.0
    assert round(data["pool"]["total_free_gb"], 1) in (5129.3, 5129.4)

    # Drive 3 is (13.03/15)=86.9% full -> "warning" band (70-90%)
    assert data["accounts"]["sub-secondary-2"]["level"] == "warning"
    assert data["accounts"]["sub-secondary-1"]["percent_used"] == 0.0


def test_static_css_is_served():
    tc = TestClient(app=main.app)
    resp = tc.get("/static/style.css")
    assert resp.status_code == 200
    assert "gauge-fill" in resp.text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
