"""Tests for the FastAPI plugin API router.

Uses FastAPI TestClient to verify endpoint responses, validation, and
serialization without needing a live dashboard server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import pytest

try:
    from fastapi.testclient import TestClient
    HAS_TESTCLIENT = True
except ImportError:
    HAS_TESTCLIENT = False

pytestmark = pytest.mark.skipif(not HAS_TESTCLIENT, reason="FastAPI TestClient not available")


def _make_app(hermes_home: Path) -> "fastapi.FastAPI":
    """Wrap the plugin router in a bare FastAPI app for testing."""
    from fastapi import FastAPI

    # plugin_api.py sits alongside hermes_summarization_calendar/ in dashboard/
    import plugin_api as ledger_mod  # type: ignore[import-not-found]
    ledger_router = ledger_mod.router

    # Set HERMES_HOME so the plugin discovers our test data
    os.environ["HERMES_HOME"] = str(hermes_home)

    app = FastAPI()
    app.include_router(ledger_router, prefix="/api/plugins/summarization-calendar")
    return app


class TestHealthEndpoint:
    def test_health_returns_ok(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["plugin_name"] == "summarization-calendar"
        assert data["version"] == "1.2.3"
        assert data["profiles_discovered"] == 2
        assert data["readable_sources"] == 2
        assert data["cron_readable"] is True

    def test_health_no_absolute_paths(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/health")
        data = resp.json()
        # Ensure no absolute paths leak into response
        raw = str(data)
        assert str(home) not in raw, f"Absolute path leaked: {raw}"


class TestMonthEndpoint:
    def test_valid_month(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/month?year=2026&month=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["year"] == 2026
        assert data["month"] == 3
        assert len(data["days"]) == 31

    def test_invalid_month(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/month?year=2026&month=13")
        assert resp.status_code == 422  # FastAPI Query validation

    def test_invalid_year(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/month?year=1800&month=3")
        assert resp.status_code == 422

    def test_day_cell_structure(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/month?year=2026&month=3")
        data = resp.json()
        for cell in data["days"]:
            assert "date" in cell
            assert "active" in cell
            assert "session_count" in cell
            assert "cron_run_count" in cell
            assert "has_recap" in cell

    def test_march_8_active(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/month?year=2026&month=3")
        data = resp.json()
        march_8 = next((d for d in data["days"] if d["date"] == "2026-03-08"), None)
        assert march_8 is not None
        assert march_8["active"] is True


class TestDayEndpoint:
    def test_valid_day(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        assert resp.status_code == 200
        data = resp.json()
        assert data["date"] == "2026-03-08"
        assert "chicago_midnight_utc" in data
        assert "chicago_next_midnight_utc" in data
        assert isinstance(data["sessions"], list)
        assert isinstance(data["cron_runs"], list)
        assert isinstance(data["source_fingerprint"], str)

    def test_session_metadata_only(self, test_hermes_home):
        """Sessions must NOT contain content, system_prompt, or DB paths."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        for s in data["sessions"]:
            assert "content" not in s
            assert "system_prompt" not in s
            assert "reasoning" not in s
            # Should have the expected fields
            assert "session_id" in s
            assert "profile" in s
            assert "source" in s
            assert "model" in s
            assert "title" in s
            assert "message_count" in s
            assert "tool_call_count" in s

    def test_plugin_session_excluded(self, test_hermes_home):
        """Sessions with source='summarization-calendar' must be excluded."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert "20260308_recap_dd" not in session_ids

    def test_legacy_plugin_session_excluded(self, test_hermes_home):
        """Legacy v1.1.0 source='daily-ledger' sessions must be excluded too.

        Pre-rename installs carry plugin-internal sessions tagged
        'daily-ledger'; after the v1.2.0 rename those must not leak into the
        calendar inventory or recap inputs.
        """
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert "20260308_recap_legacy" not in session_ids

    def test_tool_session_included(self, test_hermes_home):
        """Sessions with source='tool' must NOT be excluded."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert "20260308_tool_ff" in session_ids

    def test_invalid_date_format(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-3-8")
        assert resp.status_code == 400

    def test_invalid_date_bad_month(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-13-01")
        assert resp.status_code == 400

    def test_fingerprint_starts_with_sha256(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        assert data["source_fingerprint"].startswith("sha256:")

    def test_chicago_boundaries_correct(self, test_hermes_home):
        """Spring-forward day: 23h window."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-03-08")
        data = resp.json()
        # March 8 spring forward: midnight CST = 06:00 UTC -> next midnight CDT = 05:00+1 day
        assert data["chicago_midnight_utc"] == "2026-03-08T06:00:00Z"
        assert data["chicago_next_midnight_utc"] == "2026-03-09T05:00:00Z"

    def test_fall_back_boundaries(self, test_hermes_home):
        """Fall-back day: 25h window."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.get("/api/plugins/summarization-calendar/day?date=2026-11-01")
        data = resp.json()
        assert data["chicago_midnight_utc"] == "2026-11-01T05:00:00Z"
        assert data["chicago_next_midnight_utc"] == "2026-11-02T06:00:00Z"
