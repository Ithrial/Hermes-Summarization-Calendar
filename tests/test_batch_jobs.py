"""Tests for durable batch job storage.

Uses in-memory tmp_path ledger roots. Tests cover:
- Path/ID/date validation
- Duplicate/blank/max count rejection
- Atomic lifecycle, counts, timestamps, member order
- Invalid transitions, unknown members, status derivation
- List ordering/bounds, error sanitation
- Malformed-file resilience, recovery
- Lock serialization / lost-update prevention
- Identity compatibility (dots/spaces/hyphens)
- regenerate_current roundtrip
- cross-date list isolation
- traceback/control/error sanitation
- bool/invalid list limits
- all-skip and mixed complete+skip derivation
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import hermes_summarization_calendar.batch_jobs as batch_jobs


DATE = "2026-03-08"
BATCH_ID = "test-batch-001"


def make_member(profile: str, session_id: str) -> dict:
    return {"profile": profile, "session_id": session_id}


def make_members(count: int) -> list[dict]:
    return [make_member(f"profile-{i}", f"session-{i}") for i in range(count)]


# ---------------------------------------------------------------------
# Basic creation and loading
# ---------------------------------------------------------------------

def test_create_batch_job_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    assert batch["schema_version"] == 1
    assert batch["batch_id"] == BATCH_ID
    assert batch["date"] == DATE
    assert batch["status"] == "queued"
    assert batch["total"] == 3
    assert batch["completed"] == 0
    assert batch["failed"] == 0
    assert batch["skipped"] == 0
    assert batch["created_at"] is not None
    assert batch["started_at"] is None
    assert batch["finished_at"] is None
    assert batch["current"] is None
    assert len(batch["members"]) == 3

    # Member order preserved
    assert batch["members"][0]["profile"] == "profile-0"
    assert batch["members"][1]["profile"] == "profile-1"
    assert batch["members"][2]["profile"] == "profile-2"

    # Members start queued
    for m in batch["members"]:
        assert m["status"] == "queued"
        assert m["error"] is None
        assert m["version_id"] is None


def test_load_batch_job_returns_none_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    assert batch_jobs.load_batch_job(root, DATE, BATCH_ID) is None


def test_load_batch_job_returns_job_when_exists(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    created = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["batch_id"] == BATCH_ID
    assert loaded["date"] == DATE


def test_list_batch_jobs_empty_when_no_jobs(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    assert batch_jobs.list_batch_jobs(root, DATE) == []


def test_list_batch_jobs_returns_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    b1 = batch_jobs.create_batch_job(root, DATE, "batch-1", members)
    time.sleep(0.01)
    b2 = batch_jobs.create_batch_job(root, DATE, "batch-2", members)
    time.sleep(0.01)
    b3 = batch_jobs.create_batch_job(root, DATE, "batch-3", members)

    jobs = batch_jobs.list_batch_jobs(root, DATE, limit=10)
    assert len(jobs) == 3
    assert jobs[0]["batch_id"] == "batch-3"  # newest first
    assert jobs[1]["batch_id"] == "batch-2"
    assert jobs[2]["batch_id"] == "batch-1"


def test_list_batch_jobs_respects_limit(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    for i in range(5):
        batch_jobs.create_batch_job(root, DATE, f"batch-{i}", members)

    jobs = batch_jobs.list_batch_jobs(root, DATE, limit=2)
    assert len(jobs) == 2


# ---------------------------------------------------------------------
# Validation: dates, paths, IDs
# ---------------------------------------------------------------------

def test_rejects_invalid_date_format(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        batch_jobs.create_batch_job(root, "2026-3-8", "id", members)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        batch_jobs.create_batch_job(root, "03-08-2026", "id", members)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        batch_jobs.create_batch_job(root, "20260308", "id", members)

    with pytest.raises(ValueError, match="real calendar date"):
        batch_jobs.create_batch_job(root, "2026-02-30", "id", members)


def test_rejects_invalid_batch_id(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "", members)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "batch with spaces", members)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "batch/with/slash", members)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "batch\\with\\backslash", members)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "batch:with:colon", members)

    with pytest.raises(ValueError, match="batch_id"):
        batch_jobs.create_batch_job(root, DATE, "batch.dot", members)


def test_rejects_duplicate_composite_identity(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = [
        make_member("profile", "session-1"),
        make_member("profile", "session-1"),  # duplicate
    ]

    with pytest.raises(ValueError, match="duplicate"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)


def test_rejects_empty_profile_or_session_id(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="profile"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": "", "session_id": "s1"}])

    with pytest.raises(ValueError, match="session_id"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": "p1", "session_id": ""}])


def test_rejects_members_below_minimum(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="members count"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [])


def test_rejects_members_above_maximum(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(101)

    with pytest.raises(ValueError, match="members count"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)


# ---------------------------------------------------------------------
# Atomic lifecycle
# ---------------------------------------------------------------------

def test_start_batch_job_transitions_to_running(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    created = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    started = batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    assert started["status"] == "running"
    assert started["started_at"] is not None
    assert started["created_at"] == created["created_at"]


def test_start_batch_job_rejects_non_queued(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    with pytest.raises(ValueError, match="not found"):
        batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    with pytest.raises(ValueError, match="not in queued status"):
        batch_jobs.start_batch_job(root, DATE, BATCH_ID)


def test_update_batch_member_status_changes(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "running"
    )
    assert updated["members"][0]["status"] == "running"
    assert updated["current"] == {"profile": "profile-0", "session_id": "session-0"}

    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-001"
    )
    assert updated["members"][0]["status"] == "completed"
    assert updated["members"][0]["version_id"] == "ver-001"
    assert updated["completed"] == 1

    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "failed",
        error="test error"
    )
    assert updated["members"][1]["status"] == "failed"
    assert updated["failed"] == 1

    assert updated["members"][2]["status"] == "queued"


def test_update_batch_member_rejects_invalid_transition(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-0"
    )

    with pytest.raises(ValueError, match="already in terminal status"):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            "profile-0", "session-0",
            "failed"
        )

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "running"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "completed",
        version_id="ver-1"
    )

    with pytest.raises(ValueError, match="already in terminal status"):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            "profile-1", "session-1",
            "running"
        )


def test_update_batch_member_rejects_unknown_member(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    with pytest.raises(ValueError, match="not found"):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            "unknown-profile", "unknown-session",
            "running"
        )


def test_finalize_batch_job_derives_status(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    for i in range(3):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "completed"
    assert finalized["finished_at"] is not None


def test_finalize_batch_job_partial_status(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-2", "session-2",
        "failed",
        error="some error"
    )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "partial"


def test_finalize_batch_job_failed_status(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "failed",
            error=f"error-{i}"
        )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "failed"


def test_finalized_batch_finalize_is_idempotent_noop(tmp_path: Path) -> None:
    """v1.2.1: finalizing an already-finalized batch is a no-op, not an error.

    Live race (testbed errors.log 18:05:38): the orchestrator's finally-block
    finalize and a concurrent finalization both landed; the second raised
    'already finalized', was caught, and surfaced as 'summary failed: unknown'.
    A batch already in a terminal state must simply be returned as-is.
    """
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )
    first = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert first["status"] == "completed"
    finished_at = first["finished_at"]

    # Second finalize (the double-finalize race): no raise, same terminal state,
    # counts and timestamp untouched.
    second = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert second["status"] == "completed"
    assert second["finished_at"] == finished_at
    assert second["completed"] == 2
    assert second["failed"] == 0
    assert second["skipped"] == 0
    assert second["current"] is None

    # On-disk state is byte-stable across the no-op finalize.
    on_disk = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert on_disk["status"] == "completed"
    assert on_disk["finished_at"] == finished_at


# ---------------------------------------------------------------------
# Error sanitation
# ---------------------------------------------------------------------

def test_error_sanitizes_control_characters(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    error_with_control = "error\nwith\tcontrol\x00chars"
    batch = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "failed",
        error=error_with_control
    )

    sanitized = batch["members"][0]["error"]
    assert sanitized is not None
    assert "\n" in sanitized  # newline kept
    assert "\t" in sanitized  # tab kept
    assert "\x00" not in sanitized  # null removed


def test_error_capped_at_maximum_length(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    long_error = "x" * 600
    batch = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "failed",
        error=long_error
    )

    sanitized = batch["members"][0]["error"]
    assert sanitized is not None
    assert len(sanitized) <= 500


def test_error_empty_for_non_failed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-001"
    )

    assert batch["members"][0]["error"] is None


# ---------------------------------------------------------------------
# Malformed-file resilience
# ---------------------------------------------------------------------

def test_load_rejects_non_json_file(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    path.write_text("not json")

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is None


def test_load_rejects_missing_fields(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    data = json.loads(path.read_text())
    del data["members"]
    path.write_text(json.dumps(data))

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is None


def test_list_ignores_malformed_files(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    batch_jobs.create_batch_job(root, DATE, "valid-batch", members)

    bad_path = root / "batch-jobs" / DATE / "bad-batch.json"
    bad_path.write_text("garbage")

    jobs = batch_jobs.list_batch_jobs(root, DATE)
    assert len(jobs) == 1
    assert jobs[0]["batch_id"] == "valid-batch"


def test_load_ignores_wrong_date(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    loaded = batch_jobs.load_batch_job(root, "2026-03-09", BATCH_ID)
    assert loaded is None


# ---------------------------------------------------------------------
# Skipped statuses
# ---------------------------------------------------------------------

def test_skipped_statuses_allowed_and_counted(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(4)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )
    for i in range(2, 4):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "skipped_current",
            error="skipped due to dependency"
        )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "completed"  # skips allowed
    assert finalized["skipped"] == 2


def test_skipped_running_allowed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-0"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "skipped_running",
        error="dep failed"
    )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    # skipped_running is a skip, NOT a failure — so this is completed
    assert finalized["status"] == "completed"


# ---------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------

def test_recover_stale_batch_jobs_marks_failed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)

    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    data = json.loads(path.read_text())
    data["status"] = "running"
    data["started_at"] = data["created_at"]
    data["members"][0]["status"] = "running"
    data["members"][1]["status"] = "queued"
    path.write_text(json.dumps(data))

    recovered = batch_jobs.recover_stale_batch_jobs(root)

    assert len(recovered) == 1
    assert f"{DATE}:{BATCH_ID}" in recovered

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["status"] == "failed"
    assert loaded["finished_at"] is not None
    assert loaded["members"][0]["status"] == "failed"
    assert loaded["members"][1]["status"] == "failed"
    assert "interrupted" in (loaded["members"][0]["error"] or "").lower()
    assert "interrupted" in (loaded["members"][1]["error"] or "").lower()


def test_recover_stale_batch_jobs_ignores_terminal_batches(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)

    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )
    batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)

    recovered = batch_jobs.recover_stale_batch_jobs(root)
    assert len(recovered) == 0

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded["status"] == "completed"


# ---------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------

def test_same_batch_id_different_dates_allowed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    batch1 = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch2 = batch_jobs.create_batch_job(root, "2026-03-09", BATCH_ID, members)

    assert batch1["date"] == "2026-03-08"
    assert batch2["date"] == "2026-03-09"


def test_batch_job_persists_across_loads(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    reloaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert reloaded is not None
    assert reloaded["batch_id"] == BATCH_ID
    assert reloaded["members"][0]["profile"] == "profile-0"


def test_atomic_write_preserves_permissions(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------
# Additional tests: regenerate_current roundtrip
# ---------------------------------------------------------------------

def test_regenerate_current_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members, regenerate_current=True)
    assert batch["regenerate_current"] is True

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["regenerate_current"] is True

    batch2 = batch_jobs.create_batch_job(root, DATE, "batch-no-regen", members)
    assert batch2["regenerate_current"] is False


# ---------------------------------------------------------------------
# Additional tests: cross-date list isolation
# ---------------------------------------------------------------------

def test_cross_date_list_isolation(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, "date1-batch", members)
    batch_jobs.create_batch_job(root, "2026-03-09", "date2-batch", members)

    jobs_d1 = batch_jobs.list_batch_jobs(root, DATE)
    assert len(jobs_d1) == 1
    assert jobs_d1[0]["batch_id"] == "date1-batch"

    jobs_d2 = batch_jobs.list_batch_jobs(root, "2026-03-09")
    assert len(jobs_d2) == 1
    assert jobs_d2[0]["batch_id"] == "date2-batch"


# ---------------------------------------------------------------------
# Additional tests: traceback/control/error sanitation
# ---------------------------------------------------------------------

def test_traceback_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    fake_traceback = (
        "Traceback (most recent call last):\n"
        '  File "/path/to/file.py", line 42, in foo\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n"
    )
    batch = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "failed",
        error=fake_traceback
    )

    stored = batch["members"][0]["error"]
    assert stored is not None
    # Control chars stripped but printable traceback text is allowed (bounded)
    assert "\x00" not in stored
    assert len(stored) <= 500


# ---------------------------------------------------------------------
# Additional tests: bool/invalid list limits
# ---------------------------------------------------------------------

def test_list_rejects_bool_limit(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    with pytest.raises(ValueError, match="limit"):
        batch_jobs.list_batch_jobs(root, DATE, limit=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="limit"):
        batch_jobs.list_batch_jobs(root, DATE, limit=False)  # type: ignore[arg-type]


def test_list_rejects_out_of_bounds_limit(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)

    with pytest.raises(ValueError, match="limit"):
        batch_jobs.list_batch_jobs(root, DATE, limit=0)

    with pytest.raises(ValueError, match="limit"):
        batch_jobs.list_batch_jobs(root, DATE, limit=-1)

    with pytest.raises(ValueError, match="limit"):
        batch_jobs.list_batch_jobs(root, DATE, limit=21)


# ---------------------------------------------------------------------
# Additional tests: identity compatibility (dots/spaces/hyphens)
# ---------------------------------------------------------------------

def test_identity_accepts_dots_spaces_hyphens(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = [
        make_member("my.profile", "session-1"),
        make_member("profile with spaces", "sess.2"),
        make_member("profile-name_v2", "session_3"),
    ]
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    assert len(batch["members"]) == 3

    # Update by identity works
    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "my.profile", "session-1",
        "completed",
        version_id="v1"
    )
    assert updated["members"][0]["status"] == "completed"


def test_identity_rejects_non_string(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="profile"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": 123, "session_id": "s1"}])

    with pytest.raises(ValueError, match="session_id"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": "p", "session_id": None}])


def test_identity_rejects_blank_whitespace(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="profile"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": "   ", "session_id": "s1"}])


def test_identity_rejects_nul_control(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="profile"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [{"profile": "p\x00rofile", "session_id": "s1"}])


def test_identity_rejects_excessive_length(tmp_path: Path) -> None:
    root = tmp_path / "ledger"

    with pytest.raises(ValueError, match="maximum length"):
        batch_jobs.create_batch_job(root, DATE, BATCH_ID, [
            {"profile": "x" * 300, "session_id": "s1"}
        ])


# ---------------------------------------------------------------------
# Additional tests: lock serialization / lost-update prevention
# ---------------------------------------------------------------------

def test_lock_serializes_concurrent_updates(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    errors: list[Exception] = []

    def update_member(idx: int) -> None:
        try:
            batch_jobs.update_batch_member(
                root, DATE, BATCH_ID,
                f"profile-{idx}", f"session-{idx}",
                "completed",
                version_id=f"v{idx}"
            )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=update_member, args=(0,))
    t2 = threading.Thread(target=update_member, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"Concurrent updates failed: {errors}"

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["completed"] == 2


# ---------------------------------------------------------------------
# Additional tests: all-skip and mixed complete+skip derivation
# ---------------------------------------------------------------------

def test_all_skip_derives_completed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "skipped_current"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "skipped_running"
    )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "completed"  # all skips = completed
    assert finalized["skipped"] == 2


def test_mixed_complete_and_skip_derives_completed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "skipped_current"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-2", "session-2",
        "skipped_running"
    )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "completed"  # no failures
    assert finalized["completed"] == 1
    assert finalized["skipped"] == 2


# ---------------------------------------------------------------------
# Additional tests: finalize refuses queued/running members
# ---------------------------------------------------------------------

def test_finalize_refuses_queued_members(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    # Only update one member; other stays queued
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )

    with pytest.raises(ValueError, match="still queued/running"):
        batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)


def test_finalize_refuses_running_members(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    # Set both to running, finalize should refuse
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "running"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "running"
    )

    with pytest.raises(ValueError, match="still queued/running"):
        batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)


# ---------------------------------------------------------------------
# Additional tests: current pointer clearing
# ---------------------------------------------------------------------

def test_current_cleared_when_running_member_becomes_terminal(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "running"
    )
    assert updated["current"] == {"profile": "profile-0", "session_id": "session-0"}

    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )
    assert updated["current"] is None


def test_current_cleared_on_finalize(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "completed",
        version_id="v1"
    )

    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["current"] is None


# ---------------------------------------------------------------------
# Additional tests: error only on failed members; version_id only on completed
# ---------------------------------------------------------------------

def test_error_only_on_failed_members(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0",
        error="should be discarded"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "skipped_current",
        error="should be discarded too"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-2", "session-2",
        "failed",
        error="real failure"
    )

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["members"][0]["error"] is None  # completed
    assert loaded["members"][1]["error"] is None  # skipped
    assert loaded["members"][2]["error"] == "real failure"  # failed


def test_version_id_only_on_completed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "failed",
        error="err",
        version_id="should-not-persist"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-2", "session-2",
        "skipped_current",
        version_id="also-nope"
    )

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["members"][0]["version_id"] == "v0"  # completed keeps it
    assert loaded["members"][1]["version_id"] is None  # failed loses it
    assert loaded["members"][2]["version_id"] is None  # skipped loses it


# ---------------------------------------------------------------------
# Additional tests: recovery preserves terminal members
# ---------------------------------------------------------------------

def test_recovery_preserves_terminal_members(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    # Complete member 0, leave 1 and 2 in-flight
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="v0"
    )
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "running"
    )

    # Simulate crash: batch stays running
    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    data = json.loads(path.read_text())
    data["status"] = "running"
    path.write_text(json.dumps(data))

    recovered = batch_jobs.recover_stale_batch_jobs(root)
    assert len(recovered) == 1

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    # Terminal member preserved
    assert loaded["members"][0]["status"] == "completed"
    assert loaded["members"][0]["version_id"] == "v0"
    assert loaded["members"][0]["error"] is None
    # Running/queued members marked failed
    assert loaded["members"][1]["status"] == "failed"
    assert loaded["members"][2]["status"] == "failed"


# ---------------------------------------------------------------------
# Additional tests: recovery clears current and recalculates counts
# ---------------------------------------------------------------------

def test_recovery_clears_current_and_counts(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "running"
    )

    # Simulate crash state with current set
    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    data = json.loads(path.read_text())
    data["current"] = {"profile": "profile-0", "session_id": "session-0"}
    data["status"] = "running"
    path.write_text(json.dumps(data))

    batch_jobs.recover_stale_batch_jobs(root)

    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["current"] is None
    assert loaded["failed"] == 2
    assert loaded["completed"] == 0


# ---------------------------------------------------------------------
# Additional tests: malformed file resilience in recovery
# ---------------------------------------------------------------------

def test_recovery_skips_malformed_files(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    # Create a malformed file alongside
    bad_path = root / "batch-jobs" / DATE / "bad-batch.json"
    bad_path.write_text("not json at all")

    recovered = batch_jobs.recover_stale_batch_jobs(root)
    assert len(recovered) == 1
    assert f"{DATE}:{BATCH_ID}" in recovered


# ---------------------------------------------------------------------
# Additional tests: top-level batch-jobs symlink recovery refusal
# ---------------------------------------------------------------------

def test_recovery_refuses_symlinked_batch_jobs_dir(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    root.mkdir(parents=True, exist_ok=True)
    real_dir = tmp_path / "real-batch-jobs"
    real_dir.mkdir()
    (root / "batch-jobs").symlink_to(real_dir)

    recovered = batch_jobs.recover_stale_batch_jobs(root)
    assert recovered == []


# ---------------------------------------------------------------------
# Additional tests: list sorting with deterministic tie-breaker
# ---------------------------------------------------------------------

def test_list_sorting_deterministic_tiebreaker(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)

    # Create batches that share the same created_at timestamp
    batch_jobs.create_batch_job(root, DATE, "beta-batch", members)
    batch_jobs.create_batch_job(root, DATE, "alpha-batch", members)

    # Force identical created_at
    beta_path = root / "batch-jobs" / DATE / "beta-batch.json"
    alpha_path = root / "batch-jobs" / DATE / "alpha-batch.json"
    beta_data = json.loads(beta_path.read_text())
    alpha_data = json.loads(alpha_path.read_text())
    shared_ts = "2026-03-08T12:00:00Z"
    beta_data["created_at"] = shared_ts
    alpha_data["created_at"] = shared_ts
    beta_path.write_text(json.dumps(beta_data))
    alpha_path.write_text(json.dumps(alpha_data))

    jobs = batch_jobs.list_batch_jobs(root, DATE)
    # Same created_at -> tie-break by batch_id descending (reverse=True)
    assert jobs[0]["batch_id"] == "beta-batch"
    assert jobs[1]["batch_id"] == "alpha-batch"


# ---------------------------------------------------------------------
# Additional tests: post-update count assertions
# ---------------------------------------------------------------------

def test_post_update_counts_accurate(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(4)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)

    batch_jobs.update_batch_member(root, DATE, BATCH_ID, "profile-0", "session-0", "completed", version_id="v0")
    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["completed"] == 1
    assert loaded["failed"] == 0
    assert loaded["skipped"] == 0

    batch_jobs.update_batch_member(root, DATE, BATCH_ID, "profile-1", "session-1", "failed", error="err")
    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["completed"] == 1
    assert loaded["failed"] == 1
    assert loaded["skipped"] == 0

    batch_jobs.update_batch_member(root, DATE, BATCH_ID, "profile-2", "session-2", "skipped_current")
    loaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert loaded is not None
    assert loaded["completed"] == 1
    assert loaded["failed"] == 1
    assert loaded["skipped"] == 1
