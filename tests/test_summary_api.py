"""API contract tests for session summaries and summary-only roll-ups."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import plugin_api as api
from hermes_daily_ledger.inventory import build_day_inventory, discover_all
from hermes_daily_ledger.rollup_orchestrator import build_rollup_inputs
from hermes_daily_ledger.session_storage import save_rollup, save_session_summary
from hermes_daily_ledger.summary_jobs import (
    _reset_for_tests,
    acquire_rollup_job,
    acquire_session_job,
)


DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"


class FakeThread:
    def __init__(self, *, target, args=(), kwargs=None, **_other):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self) -> None:
        return None


@pytest.fixture
def client(test_hermes_home, tmp_path: Path, monkeypatch):
    home, _ = test_hermes_home
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LEDGER_ROOT", str(ledger_root))
    _reset_for_tests()
    with api._worker_lock:
        api._worker_pool.clear()
    api._startup_done = False
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/daily-ledger")
    yield TestClient(app), home, ledger_root, monkeypatch
    _reset_for_tests()
    with api._worker_lock:
        api._worker_pool.clear()


def test_day_enriches_each_session_with_summary_status_without_content(client) -> None:
    http, _home, _root, _ = client
    response = http.get(f"/api/plugins/daily-ledger/day?date={DATE}")
    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert sessions
    for session in sessions:
        assert session["source_fingerprint"].startswith("sha256:")
        assert session["summary_status"]["exists"] is False
        assert "content" not in session
        assert "path" not in str(session["summary_status"]).lower()


def test_get_session_summary_returns_safe_data_and_versions(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    session = next(
        item for item in inventory.sessions
        if item.profile == PROFILE and item.session_id == SESSION_ID
    )
    version = save_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        session.title,
        {"summary": "Stored summary", "key_points": []},
        session.source_fingerprint,
        ledger_root=root,
    )

    response = http.get(
        "/api/plugins/daily-ledger/session-summary",
        params={"date": DATE, "profile": PROFILE, "session_id": SESSION_ID},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is True
    assert data["stale"] is False
    assert data["data"]["summary"] == "Stored summary"
    assert data["versions"][0]["version_id"] == version.version_id
    serialized = str(data).lower()
    assert str(home).lower() not in serialized
    assert "transcript" not in serialized
    assert "messages" not in serialized


def test_unknown_and_invalid_composite_identities_are_rejected(client) -> None:
    http, _home, _root, _ = client
    unknown = http.get(
        "/api/plugins/daily-ledger/session-summary",
        params={"date": DATE, "profile": PROFILE, "session_id": "unknown"},
    )
    assert unknown.status_code == 404

    for profile, session_id in (("../default", SESSION_ID), (PROFILE, "../../etc")):
        response = http.get(
            "/api/plugins/daily-ledger/session-summary",
            params={"date": DATE, "profile": profile, "session_id": session_id},
        )
        assert response.status_code == 400


def test_post_session_summary_queues_and_same_identity_conflicts(client) -> None:
    http, _home, _root, monkeypatch = client
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    first = http.post(
        "/api/plugins/daily-ledger/session-summary",
        params={"date": DATE, "profile": PROFILE, "session_id": SESSION_ID},
        json={"force_regenerate": False},
    )
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    second = http.post(
        "/api/plugins/daily-ledger/session-summary",
        params={"date": DATE, "profile": PROFILE, "session_id": SESSION_ID},
        json={"force_regenerate": False},
    )
    assert second.status_code == 409


def test_existing_summary_requires_explicit_force(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    session = next(
        item for item in build_day_inventory(DATE, profiles, cron_roots).sessions
        if item.profile == PROFILE and item.session_id == SESSION_ID
    )
    save_session_summary(
        DATE, PROFILE, SESSION_ID, session.title,
        {"summary": "existing", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    response = http.post(
        "/api/plugins/daily-ledger/session-summary",
        params={"date": DATE, "profile": PROFILE, "session_id": SESSION_ID},
        json={"force_regenerate": False},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "summary_already_exists"


def test_session_rollback_route_restores_selected_version(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    session = next(
        item for item in build_day_inventory(DATE, profiles, cron_roots).sessions
        if item.profile == PROFILE and item.session_id == SESSION_ID
    )
    first = save_session_summary(
        DATE, PROFILE, SESSION_ID, session.title,
        {"summary": "first", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    save_session_summary(
        DATE, PROFILE, SESSION_ID, session.title,
        {"summary": "second", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    response = http.post(
        "/api/plugins/daily-ledger/session-summary/rollback",
        params={
            "date": DATE,
            "profile": PROFILE,
            "session_id": SESSION_ID,
            "version": first.version_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["version_id"] == first.version_id


def test_rollup_get_and_post_contract(client) -> None:
    http, home, root, monkeypatch = client
    profiles, cron_roots = discover_all(home)
    session = build_day_inventory(DATE, profiles, cron_roots).sessions[0]
    save_session_summary(
        DATE, session.profile, session.session_id, session.title,
        {"summary": "available", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    empty = http.get(
        "/api/plugins/daily-ledger/rollup", params={"date": DATE}
    )
    assert empty.status_code == 200
    assert empty.json()["exists"] is False

    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    queued = http.post(
        "/api/plugins/daily-ledger/rollup",
        params={"date": DATE},
        json={"force_regenerate": False},
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"


def test_month_badge_prefers_new_rollup_and_detects_staleness(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    session = build_day_inventory(DATE, profiles, cron_roots).sessions[0]
    save_session_summary(
        DATE, session.profile, session.session_id, session.title,
        {"summary": "available", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    inputs = build_rollup_inputs(DATE, profiles, cron_roots, root)
    save_rollup(
        DATE,
        {"overall_recap": "current", "included_sessions": [], "coverage": {"included": 1, "active": len(inputs.active_identities)}},
        inputs.source_fingerprint,
        ledger_root=root,
    )

    fresh = http.get("/api/plugins/daily-ledger/month?year=2026&month=3")
    march_8 = next(day for day in fresh.json()["days"] if day["date"] == DATE)
    assert march_8["has_recap"] is True
    assert march_8["recap_stale"] is False

    with sqlite3.connect(home / "state.db") as conn:
        conn.execute(
            "UPDATE messages SET content = content || ' changed' WHERE session_id = ?",
            (session.session_id,),
        )
        conn.commit()
    stale = http.get("/api/plugins/daily-ledger/month?year=2026&month=3")
    march_8 = next(day for day in stale.json()["days"] if day["date"] == DATE)
    assert march_8["has_recap"] is True
    assert march_8["recap_stale"] is True


def test_session_rollback_recovers_stale_prior_process_job(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    session = next(
        item for item in build_day_inventory(DATE, profiles, cron_roots).sessions
        if item.profile == PROFILE and item.session_id == SESSION_ID
    )
    version = save_session_summary(
        DATE, PROFILE, SESSION_ID, session.title,
        {"summary": "version", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    assert api._startup_done is False
    assert acquire_session_job(DATE, PROFILE, SESSION_ID, root) is not None

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/rollback",
        params={
            "date": DATE,
            "profile": PROFILE,
            "session_id": SESSION_ID,
            "version": version.version_id,
        },
        json={},
    )
    assert response.status_code == 200


def test_session_rollback_rejects_running_generation(client) -> None:
    http, home, root, _ = client
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    session = next(
        item for item in inventory.sessions
        if item.profile == PROFILE and item.session_id == SESSION_ID
    )
    version = save_session_summary(
        DATE, PROFILE, SESSION_ID, session.title,
        {"summary": "version", "key_points": []},
        session.source_fingerprint, ledger_root=root,
    )
    api._on_startup()
    assert acquire_session_job(DATE, PROFILE, SESSION_ID, root) is not None

    response = http.post(
        "/api/plugins/daily-ledger/session-summary/rollback",
        params={
            "date": DATE,
            "profile": PROFILE,
            "session_id": SESSION_ID,
            "version": version.version_id,
        },
        json={},
    )
    assert response.status_code == 409


def test_rollup_rollback_rejects_running_generation(client) -> None:
    http, _home, root, _ = client
    version = save_rollup(
        DATE,
        {"overall_recap": "version", "coverage": {"included": 1, "active": 1}},
        "sha256:rollup",
        ledger_root=root,
    )
    api._on_startup()
    assert acquire_rollup_job(DATE, root) is not None

    response = http.post(
        "/api/plugins/daily-ledger/rollup/rollback",
        params={"date": DATE, "version": version.version_id},
        json={},
    )
    assert response.status_code == 409
