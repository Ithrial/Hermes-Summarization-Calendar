"""Tests for batch summary API routes.

Tests POST /session-summary/batch, GET /session-summary/batch, and GET /session-summary/batches.

Uses tmp_path ledger roots and FakeThread objects. No real session DB access,
no auxiliary model inference, no live worker spawning.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import plugin_api as api
from hermes_daily_ledger.inventory import build_day_inventory, discover_all
from hermes_daily_ledger.session_storage import save_session_summary


DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"


class FakeThread:
    def __init__(self, *, target, args=(), kwargs=None, **_other):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self._started = False

    def start(self) -> None:
        self._started = True

    def is_alive(self) -> bool:
        return self._started


@pytest.fixture
def client(test_hermes_home, tmp_path: Path, monkeypatch):
    home, _ = test_hermes_home
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LEDGER_ROOT", str(ledger_root))
    with api._worker_lock:
        api._worker_pool.clear()
    api._startup_done = False
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/daily-ledger")
    yield TestClient(app), home, ledger_root, monkeypatch
    with api._worker_lock:
        api._worker_pool.clear()


def make_session_summary(date: str, profile: str, session_id: str, ledger_root: Path, home: Path) -> None:
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(date, profiles, cron_roots)
    session = next(
        item for item in inventory.sessions
        if item.profile == profile and item.session_id == session_id
    )
    save_session_summary(
        date, profile, session_id, session.title,
        {"summary": "test summary", "key_points": []},
        session.source_fingerprint, ledger_root=ledger_root,
    )


# ---------------------------------------------------------------------
# POST /session-summary/batch
# ---------------------------------------------------------------------


def test_post_batch_summary_valid_202_returns_batch_object(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    # Get first two sessions for batch
    sessions = inventory.sessions[:2]

    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members, "regenerate_current": False},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["date"] == DATE
    assert body["total"] == 2
    assert body["batch_id"].startswith("batch-")
    assert len(body["members"]) == 2
    assert body["created_at"] is not None
    # Verify worker was started
    assert len(api._worker_pool) == 1
    pool_key = list(api._worker_pool.keys())[0]
    assert pool_key.startswith("batch:")
    # Verify batch job file was created
    batch_file = root / "batch-jobs" / DATE / f"{body['batch_id']}.json"
    assert batch_file.is_file()


def test_post_batch_summary_empty_sessions_rejected(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": [], "regenerate_current": False},
    )
    # Route handler explicitly returns 400 for empty sessions
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_sessions"


def test_post_batch_summary_missing_body_rejected(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={},  # missing sessions
    )
    # FastAPI returns 422 for Pydantic validation failures
    assert response.status_code == 422


def test_post_batch_summary_over_100_members_rejected(client) -> None:
    http, _home, _root, _ = client
    members = [
        {"profile": f"p{i}", "session_id": f"s{i}"}
        for i in range(101)
    ]
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_member_count"


def test_post_batch_summary_duplicate_identity_rejected(client) -> None:
    http, _home, _root, _ = client
    members = [
        {"profile": PROFILE, "session_id": SESSION_ID},
        {"profile": PROFILE, "session_id": SESSION_ID},  # duplicate
    ]
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "duplicate_identity"


def test_post_batch_summary_blank_profile_rejected(client) -> None:
    http, _home, _root, _ = client
    members = [
        {"profile": "", "session_id": SESSION_ID},
    ]
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    assert response.status_code == 400
    assert "invalid_member" in response.json()["detail"]["error"]


def test_post_batch_summary_blank_session_id_rejected(client) -> None:
    http, _home, _root, _ = client
    members = [
        {"profile": PROFILE, "session_id": ""},
    ]
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    assert response.status_code == 400
    assert "invalid_member" in response.json()["detail"]["error"]


def test_post_batch_summary_identity_absent_for_date_rejected(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    # Get existing sessions for the test date from the fixture
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    if not inventory.sessions:
        # Skip test if no sessions exist
        pytest.skip("No sessions found for test date")

    # Use a session_id that doesn't exist
    fake_session_id = "20260308_999999_zzz"
    members = [
        {"profile": PROFILE, "session_id": fake_session_id},
    ]

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "identity_absent"

def test_post_batch_summary_worker_capacity_503(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    # Fill up worker pool with dummy entries
    with api._worker_lock:
        for i in range(api._MAX_CONCURRENCY):
            api._worker_pool[f"dummy-{i}"] = FakeThread(target=lambda: None)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    assert response.status_code == 503
    assert "worker_capacity" in response.json()["detail"]["error"]


def test_post_batch_summary_invalid_date_format_rejected(client) -> None:
    http, _home, _root, _ = client
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": "2026-3-8"},  # wrong format
        json={"sessions": [{"profile": PROFILE, "session_id": SESSION_ID}]},
    )
    assert response.status_code == 400


def test_post_batch_summary_no_raw_content_in_response(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )

    assert response.status_code == 202
    body = response.json()
    serialized = str(body).lower()
    assert "messages" not in serialized
    assert "transcript" not in serialized
    assert "prompt" not in serialized
    assert "content" not in serialized


# ---------------------------------------------------------------------
# GET /session-summary/batch
# ---------------------------------------------------------------------


def test_get_batch_summary_returns_status(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    post_resp = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    batch_id = post_resp.json()["batch_id"]

    get_resp = http.get(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE, "batch_id": batch_id},
    )

    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["status"] == "queued"
    assert body["batch_id"] == batch_id
    assert body["date"] == DATE
    assert body["total"] == 2
    assert "members" in body
    assert len(body["members"]) == 2


def test_get_batch_summary_not_found_404(client) -> None:
    http, _home, _root, _ = client
    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE, "batch_id": "nonexistent"},
    )
    assert response.status_code == 404


def test_get_batch_summary_invalid_batch_id_format_400(client) -> None:
    http, _home, _root, _ = client
    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE, "batch_id": "batch/with/slash"},
    )
    assert response.status_code == 400


def test_get_batch_summary_path_traversal_rejected(client) -> None:
    http, _home, _root, _ = client
    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE, "batch_id": "../../etc/passwd"},
    )
    assert response.status_code == 400


def test_get_batch_summary_no_raw_content_in_response(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    post_resp = http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )
    batch_id = post_resp.json()["batch_id"]

    get_resp = http.get(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE, "batch_id": batch_id},
    )

    assert get_resp.status_code == 200
    body = get_resp.json()
    serialized = str(body).lower()
    assert "messages" not in serialized
    assert "transcript" not in serialized


# ---------------------------------------------------------------------
# GET /session-summary/batches
# ---------------------------------------------------------------------


def test_list_batches_returns_date_and_list(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )

    list_resp = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE},
    )

    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["date"] == DATE
    assert "batches" in body
    assert isinstance(body["batches"], list)


def test_list_batches_default_limit_20(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)

    # Create more than 20 batches
    for i in range(25):
        members = [
            {"profile": inventory.sessions[0].profile, "session_id": inventory.sessions[0].session_id}
        ]
        http.post(
            "/api/plugins/daily-ledger/session-summary/batch",
            params={"date": DATE},
            json={"sessions": members},
        )

    list_resp = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE},
    )

    assert list_resp.status_code == 200
    body = list_resp.json()
    # Should return at most 20
    assert len(body["batches"]) <= 20


def test_list_batches_strict_limit_validation(client) -> None:
    http, _home, _root, _ = client

    # limit=true (bool) should be rejected
    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE, "limit": "true"},
    )
    # FastAPI will parse "true" as string, but the query validation should reject non-int
    assert response.status_code in (400, 422)


def test_list_batches_over_max_limit_rejected(client) -> None:
    http, _home, _root, _ = client

    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE, "limit": 100},
    )
    assert response.status_code == 422  # FastAPI returns 422 for query param validation


def test_list_batches_under_min_limit_rejected(client) -> None:
    http, _home, _root, _ = client

    response = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE, "limit": 0},
    )
    assert response.status_code == 422  # FastAPI returns 422 for query param validation


def test_list_batches_no_raw_content_in_response(client) -> None:
    http, home, root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    sessions = inventory.sessions[:2]
    members = [
        {"profile": s.profile, "session_id": s.session_id}
        for s in sessions
    ]

    http.post(
        "/api/plugins/daily-ledger/session-summary/batch",
        params={"date": DATE},
        json={"sessions": members},
    )

    list_resp = http.get(
        "/api/plugins/daily-ledger/session-summary/batches",
        params={"date": DATE},
    )

    assert list_resp.status_code == 200
    body = list_resp.json()
    serialized = str(body).lower()
    assert "messages" not in serialized
    assert "transcript" not in serialized


# ---------------------------------------------------------------------
# Live loader contract
# ---------------------------------------------------------------------


def test_plugin_api_exposes_batch_routes(client) -> None:
    """Verify batch routes are discoverable via router.routes."""
    routes = {getattr(route, "path", "") for route in api.router.routes}
    required = {
        "/session-summary/batch",
        "/session-summary/batches",
    }
    # POST /session-summary/batch is the path for POST method
    # GET /session-summary/batch is the path for GET method
    # GET /session-summary/batches is the path for GET method
    assert required.issubset(routes)
