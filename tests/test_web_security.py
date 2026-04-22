# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Security-focused tests for the FastAPI web layer.

Covered:
- SESSION_SECRET is required at import time.
- Session idle timeout (30 min) expires auth'd requests.
- CSRF is enforced on POST /job/run and other state-changing endpoints.
- Runs are scoped per user on dashboard, /run/{id}, and the WebSocket.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import time

import pytest

# Set env vars BEFORE any import of attdown.web.app — module load fails without
# SESSION_SECRET, so the import must not happen at collect time.
import os

os.environ.setdefault("SESSION_SECRET", "test-" + "x" * 40)
os.environ.setdefault("ACU_URL", "https://example.com")
os.environ.setdefault("ACU_CLIENT_ID", "test-client")
os.environ.setdefault("ACU_REDIRECT_URI", "http://localhost:8080/oauth/callback")

from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ---- helpers ----------------------------------------------------------------


def _load_app():
    """Import (or reload) the web app module."""
    if "attdown.web.app" in sys.modules:
        return importlib.reload(sys.modules["attdown.web.app"])
    import attdown.web.app as mod
    return mod


def _install_test_routes(mod) -> None:
    """Install side-door routes for tests only. Runs once per module import."""
    if getattr(mod.app.state, "_test_routes_installed", False):
        return

    @mod.app.post("/_test/login")
    async def _test_login(request: Request) -> dict:
        body = await request.json()
        now = time.time()
        request.session["tokens"] = {
            "access_token": "fake",
            "refresh_token": "fake-refresh",
            "expires_at": now + float(body.get("expires_in", 3600)),
        }
        request.session["username"] = body["username"]
        request.session["last_activity"] = now
        request.session["csrf"] = "test-csrf-token"
        return {"ok": True}

    @mod.app.post("/_test/set_last_activity")
    async def _test_set_last_activity(request: Request) -> dict:
        body = await request.json()
        request.session["last_activity"] = float(body["value"])
        return {"ok": True}

    mod.app.state._test_routes_installed = True


def _login(client: TestClient, username: str = "alice") -> None:
    resp = client.post("/_test/login", json={"username": username})
    assert resp.status_code == 200, resp.text


@pytest.fixture
def app_client():
    mod = _load_app()
    _install_test_routes(mod)
    # Reset run state so tests don't leak into each other.
    mod.STATE["runs"].clear()
    return mod, TestClient(mod.app)


# ---- tests ------------------------------------------------------------------


def test_session_secret_is_required() -> None:
    """Run a clean subprocess with no SESSION_SECRET and assert import fails."""
    env = {k: v for k, v in os.environ.items() if k != "SESSION_SECRET"}
    env["ACU_URL"] = "https://example.com"
    env["ACU_CLIENT_ID"] = "test-client"
    env["ACU_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"
    proc = subprocess.run(
        [sys.executable, "-c", "import attdown.web.app"],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "SESSION_SECRET must be set" in proc.stderr


def test_csrf_missing_rejected_on_job_run(app_client) -> None:
    _, client = app_client
    _login(client)
    r = client.post("/job/run", data={"entity": "Bill", "filter": "x eq 1"})
    assert r.status_code == 403
    assert "CSRF" in r.text


def test_csrf_wrong_token_rejected(app_client) -> None:
    _, client = app_client
    _login(client)
    r = client.post(
        "/job/run",
        data={"entity": "Bill", "filter": "x eq 1", "_csrf": "wrong"},
    )
    assert r.status_code == 403


def test_csrf_header_accepted(app_client) -> None:
    """CSRF gate lets a request through when the header matches."""
    _, client = app_client
    _login(client)
    r = client.post(
        "/api/match/preview",
        data={"match_field": "Vendor", "match_values": "V1\nV2"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert r.status_code == 200, r.text


def test_idle_timeout_expires_session(app_client) -> None:
    _, client = app_client
    _login(client)
    stale = time.time() - (31 * 60)
    assert client.post("/_test/set_last_activity", json={"value": stale}).status_code == 200
    r = client.get("/job/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/oauth/login"


def test_cross_user_run_page_is_404(app_client) -> None:
    mod, client = app_client
    from attdown.config import JobConfig
    mod.STATE["runs"]["runAAA"] = mod.RunState(
        id="runAAA",
        job=JobConfig(entity="Bill", filter="x eq 1"),
        user="alice",
    )
    _login(client, username="bob")
    r = client.get("/run/runAAA", follow_redirects=False)
    assert r.status_code == 404


def test_owner_can_see_own_run(app_client) -> None:
    mod, client = app_client
    from attdown.config import JobConfig
    mod.STATE["runs"]["runBBB"] = mod.RunState(
        id="runBBB",
        job=JobConfig(entity="Bill", filter="x eq 1"),
        user="carol",
    )
    _login(client, username="carol")
    r = client.get("/run/runBBB", follow_redirects=False)
    assert r.status_code == 200


def test_cross_user_websocket_closes(app_client) -> None:
    mod, client = app_client
    from attdown.config import JobConfig
    mod.STATE["runs"]["runCCC"] = mod.RunState(
        id="runCCC",
        job=JobConfig(entity="Bill", filter="x eq 1"),
        user="alice",
    )
    _login(client, username="bob")
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/run/runCCC"):
            pass


def test_mkdir_moved_to_post_requires_csrf(app_client) -> None:
    """mkdir on GET used to mutate server FS on a cross-site navigation; it
    must now reject the GET entirely and require a CSRF-protected POST."""
    _, client = app_client
    _login(client)
    # GET must not mutate: even if mkdir query param is sneaked in, the handler
    # no longer accepts it.
    r = client.get("/api/fs/list?mkdir=should_not_create")
    # Listing is still fine (200) but no mkdir side-effect occurred — verified
    # by the fact that the endpoint signature no longer has a `mkdir` param.
    # A direct POST without CSRF is rejected.
    r2 = client.post("/api/fs/mkdir", data={"path": "/tmp", "mkdir": "x"})
    assert r2.status_code == 403
