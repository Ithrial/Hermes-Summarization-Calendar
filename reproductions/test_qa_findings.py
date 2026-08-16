"""v1.2.4 DEFECT-FIXED verification for the two QA Important findings.

These were the original QA defect reproductions (see hermes-calendar-qa/QA-REPORT.md,
findings 1 and 2). After the v1.2.4 fixes they are inverted to assert the
FIXED behavior, so a regression in either defect fails this file again.

Run from the repo root (needs the ``tests/`` conftest fixtures)::

    PYTHONPATH="tests:dashboard:scripts" \
      python -m pytest -q -p conftest reproductions/test_qa_findings.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))

import plugin_api as api  # noqa: E402
from hermes_summarization_calendar.auxiliary_runner import AuxiliaryResult  # noqa: E402
from hermes_summarization_calendar.inventory import discover_all  # noqa: E402
from hermes_summarization_calendar.session_orchestrator import generate_session_summary  # noqa: E402

DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"


class FailingThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        raise RuntimeError("failed at /private/secret/path token_live_abcdef")

    def is_alive(self):
        return False


def test_worker_start_exception_is_redacted(test_hermes_home, tmp_path, monkeypatch):
    """QA finding 2 (FIXED): the 500 must NOT disclose the raw exception.

    Original repro asserted the path and token WERE present. The fix returns
    a fixed public message and logs the exception server-side only.
    """
    home, _ = test_hermes_home
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LEDGER_ROOT", str(ledger_root))
    monkeypatch.setattr(api.threading, "Thread", FailingThread)
    with api._worker_lock:
        api._worker_pool.clear()
    api._startup_done = False

    profiles, cron_roots = discover_all(home)
    sessions = api.build_day_inventory(DATE, profiles, cron_roots).sessions[:1]
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/summarization-calendar")
    response = TestClient(app).post(
        "/api/plugins/summarization-calendar/session-summary/batch",
        params={"date": DATE},
        json={
            "sessions": [
                {"profile": item.profile, "session_id": item.session_id}
                for item in sessions
            ],
            "regenerate_current": False,
        },
    )

    assert response.status_code == 500
    message = response.json()["detail"]["message"]
    # FIXED: internals must be gone from the public response.
    assert "/private/secret/path" not in message
    assert "token_live_abcdef" not in message
    assert "RuntimeError" not in message


def test_large_session_reduces_instead_of_failing(test_hermes_home, tmp_path):
    """QA finding 1 (FIXED): a large session with >= 5 near-limit segment
    summaries now completes via hierarchical reduction instead of failing with
    'Reduction prompt exceeds size limit'.

    Original repro asserted the job FAILED at the reduction step. The fix
    packs segment summaries into prompt-sized groups and reduces level by
    level, so the same 300 KB fixture now completes.
    """
    home, _ = test_hermes_home
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute(
        "UPDATE messages SET content = ? WHERE session_id = ?",
        (("seg-" + "x" * 40_000) * 6, SESSION_ID),
    )
    conn.commit()
    conn.close()
    profiles, cron_roots = discover_all(home)
    calls = 0
    near_limit = "S" * 12_000

    def runner(*, prompt: str, **_kwargs):
        nonlocal calls
        calls += 1
        if "SEGMENT_SUMMARIES_FOR_REDUCTION" in prompt:
            return AuxiliaryResult(
                session_summaries=[],
                overall_recap="",
                raw_json={"summary": f"reduced-{calls}", "key_points": []},
            )
        return AuxiliaryResult(
            session_summaries=[],
            overall_recap="",
            raw_json={"summary": near_limit, "key_points": []},
        )

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        ledger_root=tmp_path / "ledger",
    )

    assert calls >= 5
    # FIXED: the job completes instead of failing at the reduction ceiling.
    assert status.status == "completed", status.error
    assert "Reduction prompt exceeds size limit" not in (status.error or "")
