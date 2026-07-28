"""Tests for isolated per-session orchestrator using auxiliary.compression."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from hermes_daily_ledger.auxiliary_runner import AuxiliaryResult
from hermes_daily_ledger.inventory import discover_all
from hermes_daily_ledger.session_orchestrator import generate_session_summary
from hermes_daily_ledger.session_storage import load_session_summary
from hermes_daily_ledger.summary_jobs import _reset_for_tests

DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"
TITLE = "DST migration task"


@pytest.fixture(autouse=True)
def reset_jobs():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _aux_result(
    summary: str = "Timezone behavior was checked successfully.",
    *,
    profile: str = PROFILE,
    session_id: str = SESSION_ID,
    title: str = TITLE,
) -> AuxiliaryResult:
    """Return a mock AuxiliaryResult (what the real runner returns).

    For per-session summarization, returns content-only format with
    exactly 'summary' and 'key_points' - identity fields are server-owned.
    """
    raw = {
        "summary": summary,
        "key_points": ["DST window verified"],
    }
    return AuxiliaryResult(
        session_summaries=[],
        overall_recap="",
        raw_json=raw,
    )


def test_single_session_success_uses_canonical_identity_and_title(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    prompts: list[str] = []

    def runner(*, prompt: str, **_kwargs) -> AuxiliaryResult:
        prompts.append(prompt)
        return _aux_result(title="Tried to rename this")

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        ledger_root=tmp_path / "ledger",
    )

    assert status.status == "completed"
    assert len(prompts) == 1
    # With content-only contract, identity fields (profile, session_id, title) are
    # NOT in the payload (they're server-owned canonical identity attached when saving)
    # but they ARE mentioned in the instruction as forbidden output fields
    assert "calendar_date" in prompts[0], "Prompt must contain calendar_date"
    assert "messages" in prompts[0], "Prompt must contain messages array"
    # Identity fields appear in instruction as forbidden, but NOT in payload
    # Check that identity fields are NOT in the payload section (after LEDGER_DATA_BEGIN)
    payload_start = prompts[0].find("LEDGER_DATA_BEGIN")
    if payload_start >= 0:
        payload = prompts[0][payload_start:]
        assert "profile" not in payload, "Payload must NOT contain profile field"
        assert "session_id" not in payload, "Payload must NOT contain session_id field"
        assert "title" not in payload, "Payload must NOT contain title field"
    assert "20260308_tool_ff" not in prompts[0]
    raw, meta = load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    )
    assert raw is not None and raw["summary"].startswith("Timezone behavior")
    assert meta is not None
    assert meta["title"] == TITLE
    assert meta["profile"] == PROFILE
    assert meta["session_id"] == SESSION_ID


def test_unknown_composite_identity_fails_without_artifact(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    status = generate_session_summary(
        DATE,
        "named-profile",
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=lambda prompt, **kw: _aux_result(),
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "failed"
    assert "not found" in (status.error or "").lower()
    assert load_session_summary(
        DATE, "named-profile", SESSION_ID, tmp_path / "ledger"
    ) == (None, None)


def test_chunk_identity_is_server_owned(test_hermes_home, tmp_path: Path) -> None:
    """Canonical identity (profile, session_id, title) is server-owned authority.

    With strict content-only output contract, the model cannot return
    session_id or profile fields. Identity is attached server-side from
    canonical inventory when saving. This test verifies the generation
    completes successfully and the saved artifact carries the canonical
    identity from inventory, not from any model output (which has none).
    """
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        # Model returns content-only (no identity fields)
        runner=lambda prompt, **kw: _aux_result(),
        ledger_root=tmp_path / "ledger",
    )
    # Should complete successfully - server owns identity
    assert status.status == "completed"
    raw, meta = load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    )
    assert raw is not None
    # Meta contains canonical identity from inventory, not model output
    assert meta is not None
    assert meta["session_id"] == SESSION_ID
    assert meta["profile"] == PROFILE


def test_oversized_session_reduces_only_segment_summaries(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    db = home / "state.db"
    marker = "UNIQUE_RAW_TRANSCRIPT_MARKER_"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE messages SET content = ? WHERE session_id = ?",
        (marker + ("x" * 14000), SESSION_ID),
    )
    conn.commit()
    conn.close()
    profiles, cron_roots = discover_all(home)
    prompts: list[str] = []

    def runner(*, prompt: str, **_kwargs) -> AuxiliaryResult:
        prompts.append(prompt)
        if "SEGMENT_SUMMARIES_FOR_REDUCTION" in prompt:
            assert marker not in prompt
            return _aux_result("Final reduced session summary.")
        return _aux_result(f"Validated segment {len(prompts)}.")

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        safe_ceiling=4500,
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "completed"
    assert len(prompts) > 2
    assert "SEGMENT_SUMMARIES_FOR_REDUCTION" in prompts[-1]
    raw, _ = load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    )
    assert raw is not None
    assert raw["summary"] == "Final reduced session summary."
    assert raw["segment_count"] == len(prompts) - 1


def test_invalid_reduction_falls_back_to_ordered_validated_segments(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute(
        "UPDATE messages SET content = ? WHERE session_id = ?",
        ("z" * 12000, SESSION_ID),
    )
    conn.commit()
    conn.close()
    profiles, cron_roots = discover_all(home)
    segment_number = 0

    def runner(*, prompt: str, **_kwargs) -> AuxiliaryResult:
        nonlocal segment_number
        if "SEGMENT_SUMMARIES_FOR_REDUCTION" in prompt:
            return AuxiliaryResult(error="reducer unavailable")
        segment_number += 1
        return _aux_result(f"Segment summary {segment_number}.")

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        safe_ceiling=4500,
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "completed"
    raw, _ = load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    )
    assert raw is not None
    assert "Part 1" in raw["summary"]
    assert f"Part {segment_number}" in raw["summary"]


def test_source_change_during_generation_fails_before_publish(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    db = home / "state.db"
    profiles, cron_roots = discover_all(home)
    changed = False

    def runner(**_kwargs) -> AuxiliaryResult:
        nonlocal changed
        if not changed:
            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE messages SET content = ? WHERE session_id = ?",
                ("changed while summarizer ran", SESSION_ID),
            )
            conn.commit()
            conn.close()
            changed = True
        return _aux_result()

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=runner,
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "failed"
    assert "changed during generation" in (status.error or "").lower()
    assert load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    ) == (None, None)


def test_success_does_not_modify_source_database(test_hermes_home, tmp_path: Path) -> None:
    home, _ = test_hermes_home
    db = home / "state.db"
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_mtime = db.stat().st_mtime_ns
    profiles, cron_roots = discover_all(home)

    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=lambda prompt, **kw: _aux_result(),
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "completed"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert db.stat().st_mtime_ns == before_mtime


def test_stored_raw_json_contains_no_transcript_shape(
    test_hermes_home, tmp_path: Path
) -> None:
    home, _ = test_hermes_home
    profiles, cron_roots = discover_all(home)
    status = generate_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        profiles=profiles,
        cron_roots=cron_roots,
        runner=lambda prompt, **kw: _aux_result(),
        ledger_root=tmp_path / "ledger",
    )
    assert status.status == "completed"
    raw, _ = load_session_summary(
        DATE, PROFILE, SESSION_ID, tmp_path / "ledger"
    )
    serialized = json.dumps(raw).lower()
    assert "messages" not in serialized
    assert "transcript" not in serialized
    assert "system_prompt" not in serialized
