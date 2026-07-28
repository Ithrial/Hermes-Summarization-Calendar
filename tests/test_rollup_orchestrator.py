"""Tests for summary-only daily roll-up orchestration."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from hermes_daily_ledger.auxiliary_runner import AuxiliaryResult
from hermes_daily_ledger.inventory import build_day_inventory, discover_all
from hermes_daily_ledger.rollup_orchestrator import (
    build_rollup_inputs,
    check_rollup_status,
    generate_rollup,
)
from hermes_daily_ledger.session_storage import (
    load_rollup,
    save_session_summary,
)
from hermes_daily_ledger.summary_jobs import _reset_for_tests

DATE = "2026-03-08"


@pytest.fixture(autouse=True)
def reset_jobs():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _save_current_summary(
    root: Path,
    session,
    text: str = "Saved compact summary.",
):
    return save_session_summary(
        DATE,
        session.profile,
        session.session_id,
        session.title,
        {"summary": text, "key_points": []},
        session.source_fingerprint,
        ledger_root=root,
    )


def _aux_rollup(text: str = "A useful day with partial coverage.") -> AuxiliaryResult:
    """Return a mock roll-up result (what auxiliary_runner returns)."""
    raw = {"overall_recap": text}
    return AuxiliaryResult(overall_recap=text, raw_json=raw)


def test_rollup_prompt_uses_saved_summaries_not_raw_transcripts(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    root = tmp_path / "ledger"
    session = inventory.sessions[0]
    _save_current_summary(root, session, "SAVED_SUMMARY_MARKER")
    source_db = next(p.db_path for p in profiles if p.label == session.profile)
    db_hash = hashlib.sha256(source_db.read_bytes()).hexdigest()
    prompts: list[str] = []

    def runner(*, prompt: str, **_kwargs) -> AuxiliaryResult:
        prompts.append(prompt)
        assert "SAVED_SUMMARY_MARKER" in prompt
        assert "check timezone handling" not in prompt
        assert "still debugging" not in prompt
        assert "tool call" not in prompt
        return _aux_rollup()

    status = generate_rollup(
        DATE,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        ledger_root=root,
    )
    assert status.status == "completed"
    assert len(prompts) == 1
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == db_hash

    raw, meta = load_rollup(DATE, root)
    assert raw is not None and meta is not None
    assert raw["coverage"]["included"] == 1
    assert raw["coverage"]["active"] == len(inventory.sessions)
    assert len(raw["missing_sessions"]) == len(inventory.sessions) - 1
    serialized = json.dumps(raw)
    assert "SAVED_SUMMARY_MARKER" not in serialized
    assert "check timezone handling" not in serialized


def test_rollup_with_zero_current_summaries_fails_without_calling_runner(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    called = False

    def runner(**_kwargs) -> AuxiliaryResult:
        nonlocal called
        called = True
        return _aux_rollup()

    status = generate_rollup(
        DATE,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "failed"
    assert "no current session summaries" in (status.error or "").lower()
    assert not called


def test_stale_session_summary_is_excluded_from_coverage(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    session = inventory.sessions[0]
    root = tmp_path / "ledger"
    save_session_summary(
        DATE,
        session.profile,
        session.session_id,
        session.title,
        {"summary": "old", "key_points": []},
        "sha256:stale",
        ledger_root=root,
    )

    inputs = build_rollup_inputs(DATE, profiles, cron_roots, root)
    assert inputs.coverage_included == 0
    assert inputs.coverage_active == len(inventory.sessions)
    assert any(
        item["profile"] == session.profile and item["session_id"] == session.session_id
        for item in inputs.missing_sessions
    )


def test_input_fingerprint_changes_when_another_summary_becomes_available(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    root = tmp_path / "ledger"
    _save_current_summary(root, inventory.sessions[0], "first")
    before = build_rollup_inputs(DATE, profiles, cron_roots, root)
    _save_current_summary(root, inventory.sessions[1], "second")
    after = build_rollup_inputs(DATE, profiles, cron_roots, root)
    assert before.source_fingerprint != after.source_fingerprint
    assert after.coverage_included == before.coverage_included + 1


def test_check_status_marks_rollup_stale_after_summary_version_changes(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    root = tmp_path / "ledger"
    session = inventory.sessions[0]
    _save_current_summary(root, session, "first")
    generated = generate_rollup(
        DATE,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=lambda prompt, **kw: _aux_rollup(),
        ledger_root=root,
    )
    assert generated.status == "completed"
    assert check_rollup_status(DATE, profiles, cron_roots, root)["stale"] is False

    _save_current_summary(root, session, "replacement")
    status = check_rollup_status(DATE, profiles, cron_roots, root)
    assert status["exists"] is True
    assert status["stale"] is True


def test_invalid_or_empty_rollup_is_not_published(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    root = tmp_path / "ledger"
    _save_current_summary(root, inventory.sessions[0])

    status = generate_rollup(
        DATE,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=lambda prompt, **kw: AuxiliaryResult(raw_json={"overall_recap": ""}),
        ledger_root=root,
    )
    assert status.status == "failed"
    assert load_rollup(DATE, root) == (None, None)


def test_rollup_input_contains_only_compact_cron_metadata(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    inventory = build_day_inventory(DATE, profiles, cron_roots)
    root = tmp_path / "ledger"
    _save_current_summary(root, inventory.sessions[0])
    inputs = build_rollup_inputs(DATE, profiles, cron_roots, root)
    assert inputs.cron_runs
    for run in inputs.cron_runs:
        assert set(run) <= {
            "execution_id", "job_id", "job_name", "profile", "status",
            "started_at", "finished_at",
        }
        assert "error_summary" not in run
