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
        assert data["version"] == "1.2.4"
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


class TestWorkerStartErrorHygiene:
    """v1.2.4 regression: worker-start failures must not leak internals.

    QA finding 2 and scan finding 6: raw exception text (filesystem paths,
    credential-like values) must never reach the public 500 response, and
    public job identifiers must not embed the Dashboard process ID
    (scan finding 3).
    """

    @pytest.fixture(autouse=True)
    def _clean_worker_pool(self):
        import plugin_api as api_mod  # type: ignore[import-not-found]

        with api_mod._worker_lock:
            api_mod._worker_pool.clear()
        yield
        with api_mod._worker_lock:
            api_mod._worker_pool.clear()

    @staticmethod
    def _failing_thread_factory():
        class FailingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                raise RuntimeError(
                    "failed at /private/secret/path token_live_abcdef"
                )

            def is_alive(self):
                return False

        return FailingThread

    def test_batch_worker_start_failure_has_fixed_message(self, test_hermes_home, monkeypatch):
        home, _ = test_hermes_home
        app = _make_app(home)
        import plugin_api as api_mod  # type: ignore[import-not-found]
        from hermes_summarization_calendar.inventory import discover_all  # type: ignore[import-not-found]

        # Find a real session identity in the fixture day so batch
        # validation passes and the worker-start path is reached.
        profiles, cron_roots = discover_all(home)
        sessions = api_mod.build_day_inventory("2026-03-08", profiles, cron_roots).sessions
        assert sessions, "fixture day must contain sessions"
        first = sessions[0]

        monkeypatch.setattr(api_mod.threading, "Thread", self._failing_thread_factory())
        with api_mod._worker_lock:
            api_mod._worker_pool.clear()

        client = TestClient(app)
        resp = client.post(
            "/api/plugins/summarization-calendar/session-summary/batch",
            params={"date": "2026-03-08"},
            json={
                "sessions": [
                    {"profile": first.profile, "session_id": first.session_id}
                ],
                "regenerate_current": False,
            },
        )

        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert detail["error"] == "worker_start_failed"
        body = resp.text
        assert "/private/secret/path" not in body
        assert "token_live_abcdef" not in body
        assert "Try again shortly" in detail["message"]

    def test_batch_job_id_does_not_disclose_pid(self, test_hermes_home):
        """Batch queue identifiers must be opaque random values, never PIDs."""
        home, _ = test_hermes_home
        app = _make_app(home)
        import plugin_api as api_mod  # type: ignore[import-not-found]
        from hermes_summarization_calendar.inventory import discover_all  # type: ignore[import-not-found]

        profiles, cron_roots = discover_all(home)
        sessions = api_mod.build_day_inventory("2026-03-08", profiles, cron_roots).sessions
        assert sessions, "fixture day must contain sessions"
        first = sessions[0]

        client = TestClient(app)
        resp = client.post(
            "/api/plugins/summarization-calendar/session-summary/batch",
            params={"date": "2026-03-08"},
            json={
                "sessions": [
                    {"profile": first.profile, "session_id": first.session_id}
                ],
                "regenerate_current": False,
            },
        )

        assert resp.status_code == 202
        batch_id = resp.json()["batch_id"]
        # Opaque random suffix: 16 hex chars, never the process ID (scan #3)
        suffix = batch_id.rsplit("-", 1)[-1]
        assert len(suffix) == 16
        int(suffix, 16)
        assert str(os.getpid()) not in batch_id


class TestLegacyRecapRetirement:
    """v1.2.4: legacy raw-transcript recap generation is retired (QA #3).

    The project contract requires daily roll-ups to consume only saved
    session-summary artifacts, never raw transcripts. POST /recap was the
    last route that fed raw transcripts to the model, so it now returns a
    stable 410 with no generation side effects. Read access (GET /recap,
    GET /recap/versions) and version management (POST /recap/rollback)
    remain, and existing stored recaps are never touched.
    """

    @pytest.fixture(autouse=True)
    def _clean_worker_pool(self):
        import plugin_api as api_mod  # type: ignore[import-not-found]

        with api_mod._worker_lock:
            api_mod._worker_pool.clear()
        yield
        with api_mod._worker_lock:
            api_mod._worker_pool.clear()

    def test_post_recap_returns_410_with_fixed_message(self, test_hermes_home):
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.post(
            "/api/plugins/summarization-calendar/recap",
            params={"date": "2026-03-08"},
        )

        assert resp.status_code == 410
        detail = resp.json()["detail"]
        assert detail["error"] == "recap_generation_retired"
        message = detail["message"]
        # Points callers at the supported generation paths.
        assert "session-summary/batch" in message
        assert "/rollup" in message
        # Fixed message: no exception text, no internals.
        assert "RuntimeError" not in message

    def test_post_recap_with_force_body_still_410(self, test_hermes_home):
        """Pre-v1.2.4 clients send {"force_regenerate": true}; still a clean 410."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.post(
            "/api/plugins/summarization-calendar/recap",
            params={"date": "2026-03-08"},
            json={"force_regenerate": True},
        )

        assert resp.status_code == 410
        assert resp.json()["detail"]["error"] == "recap_generation_retired"

    def test_post_recap_invalid_date_still_400(self, test_hermes_home):
        """Input validation runs before retirement, keeping 400 semantics."""
        home, _ = test_hermes_home
        app = _make_app(home)
        client = TestClient(app)

        resp = client.post(
            "/api/plugins/summarization-calendar/recap",
            params={"date": "bad-date"},
        )

        assert resp.status_code == 400

    def test_post_recap_retired_no_generation_side_effects(self, test_hermes_home, tmp_path):
        """Retirement must not acquire slots, spawn workers, or queue jobs."""
        import hermes_summarization_calendar.concurrency as conc_mod
        import plugin_api as api_mod  # type: ignore[import-not-found]

        home, _ = test_hermes_home
        os.environ["LEDGER_ROOT"] = str(tmp_path / "ledger")
        app = _make_app(home)
        client = TestClient(app)
        with conc_mod._lock_registry_lock:
            conc_mod._locks.clear()

        for _ in range(2):
            resp = client.post(
                "/api/plugins/summarization-calendar/recap",
                params={"date": "2026-03-08"},
                json={"force_regenerate": True},
            )
            assert resp.status_code == 410

        with conc_mod._lock_registry_lock:
            assert "2026-03-08" not in conc_mod._locks
        with api_mod._worker_lock:
            assert "2026-03-08" not in api_mod._worker_pool
        os.environ.pop("LEDGER_ROOT", None)

    def test_get_recap_read_access_survives(self, test_hermes_home, tmp_path):
        """Read access for existing recaps is the preserved surface."""
        home, _ = test_hermes_home
        ledger = tmp_path / "ledger"
        os.environ["LEDGER_ROOT"] = str(ledger)
        app = _make_app(home)
        client = TestClient(app)

        from hermes_summarization_calendar.recap_storage import save_recap
        save_recap(
            "2026-03-08",
            {
                "session_summaries": [
                    {"session_id": "s1", "title": "T", "summary": "S"}
                ],
                "overall_recap": "Test",
            },
            "fp1",
            ledger_root=ledger,
        )

        resp = client.get(
            "/api/plugins/summarization-calendar/recap?date=2026-03-08"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True

        versions = client.get(
            "/api/plugins/summarization-calendar/recap/versions?date=2026-03-08"
        )
        assert versions.status_code == 200
        assert len(versions.json()["versions"]) == 1

        # ...and retirement does not overwrite or delete the stored recap.
        retired = client.post(
            "/api/plugins/summarization-calendar/recap",
            params={"date": "2026-03-08"},
            json={"force_regenerate": True},
        )
        assert retired.status_code == 410

        still = client.get(
            "/api/plugins/summarization-calendar/recap?date=2026-03-08"
        )
        assert still.status_code == 200
        assert still.json()["exists"] is True
        os.environ.pop("LEDGER_ROOT", None)
