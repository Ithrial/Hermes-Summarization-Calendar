"""Tests for keyed durable session-summary and roll-up jobs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import hermes_daily_ledger.summary_jobs as jobs


DATE = "2026-03-08"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    jobs._reset_for_tests()
    yield
    jobs._reset_for_tests()


def test_same_session_conflicts_and_completion_releases(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    first = jobs.acquire_session_job(DATE, "default", "session-1", root)
    assert first is not None and first.status == "running"
    assert jobs.acquire_session_job(DATE, "default", "session-1", root) is None

    completed = jobs.complete_session_job(
        DATE, "default", "session-1", "version-1", root
    )
    assert completed.status == "completed"
    assert jobs.acquire_session_job(DATE, "default", "session-1", root) is not None


def test_different_sessions_share_bounded_global_capacity(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    for index in range(4):
        assert jobs.acquire_session_job(
            DATE, "default", f"session-{index}", root
        ) is not None
    assert jobs.acquire_session_job(DATE, "default", "session-5", root) is None


def test_session_status_path_uses_hashed_identity(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    jobs.acquire_session_job(DATE, "../profile", "../../session", root)
    files = list((root / "running" / "sessions").glob("*.json"))
    assert len(files) == 1
    assert "profile" not in files[0].name
    assert "session" not in files[0].name
    status = json.loads(files[0].read_text())
    assert status["profile"] == "../profile"
    assert status["session_id"] == "../../session"


def test_failure_is_sanitized_and_durable(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    jobs.acquire_session_job(DATE, "default", "s1", root)
    failed = jobs.fail_session_job(
        DATE,
        "default",
        "s1",
        "bad /home/alice/private token abcdef0123456789abcdef0123456789",
        root,
    )
    assert failed.status == "failed"
    assert "/home/alice" not in (failed.error or "")
    assert "abcdef0123456789abcdef0123456789" not in (failed.error or "")
    loaded = jobs.load_session_job(DATE, "default", "s1", root)
    assert loaded == failed


def test_rollup_and_session_keys_do_not_conflict(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    assert jobs.acquire_rollup_job(DATE, root) is not None
    assert jobs.acquire_rollup_job(DATE, root) is None
    assert jobs.acquire_session_job(DATE, "default", "s1", root) is not None
    completed = jobs.complete_rollup_job(DATE, "rollup-version", root)
    assert completed.status == "completed"


def test_recover_stale_running_jobs_marks_failed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    jobs.acquire_session_job(DATE, "default", "s1", root)
    jobs.acquire_rollup_job(DATE, root)
    jobs._reset_for_tests()

    recovered = jobs.recover_stale_jobs(root)
    assert len(recovered) == 2
    session = jobs.load_session_job(DATE, "default", "s1", root)
    rollup = jobs.load_rollup_job(DATE, root)
    assert session is not None and session.status == "failed"
    assert rollup is not None and rollup.status == "failed"
    assert "stale" in (session.error or "").lower()


def test_corrupt_status_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    status = jobs.acquire_session_job(DATE, "default", "s1", root)
    assert status is not None
    path = next((root / "running" / "sessions").glob("*.json"))
    path.write_text("not json")
    assert jobs.load_session_job(DATE, "default", "s1", root) is None
