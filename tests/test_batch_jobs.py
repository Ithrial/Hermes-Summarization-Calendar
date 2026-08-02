"""Tests for durable batch job storage.

Uses in-memory tmp_path ledger roots. Tests cover:
- Path/ID/date validation
- Duplicate/blank/max count rejection
- Atomic lifecycle, counts, timestamps, member order
- Invalid transitions, unknown members, status derivation
- List ordering/bounds, error sanitation
- Malformed-file resilience, recovery
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import hermes_daily_ledger.batch_jobs as batch_jobs


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
    
    # Create three batches with small delays
    b1 = batch_jobs.create_batch_job(root, DATE, "batch-1", members)
    import time
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
    
    # Start on non-existent batch -> not found error
    with pytest.raises(ValueError, match="not found"):
        batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Create batch
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    # Now start it
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Can't start an already-running batch
    with pytest.raises(ValueError, match="not in queued status"):
        batch_jobs.start_batch_job(root, DATE, BATCH_ID)


def test_update_batch_member_status_changes(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(3)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    # Start batch
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Update member 0 to running
    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "running"
    )
    assert updated["members"][0]["status"] == "running"
    assert updated["current"] == {"profile": "profile-0", "session_id": "session-0"}
    
    # Update member 0 to completed
    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-001"
    )
    assert updated["members"][0]["status"] == "completed"
    assert updated["members"][0]["version_id"] == "ver-001"
    assert updated["completed"] == 1
    
    # Update member 1 to failed
    updated = batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-1", "session-1",
        "failed",
        error="test error"
    )
    assert updated["members"][1]["status"] == "failed"
    assert updated["failed"] == 1
    
    # Member 2 still queued
    assert updated["members"][2]["status"] == "queued"


def test_update_batch_member_rejects_invalid_transition(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    # Can transition directly from queued to any status
    # (including skipping running state)
    batch_jobs.update_batch_member(
        root, DATE, BATCH_ID,
        "profile-0", "session-0",
        "completed",
        version_id="ver-0"
    )
    
    # But can't transition from completed to failed
    with pytest.raises(ValueError, match="already in terminal status"):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            "profile-0", "session-0",
            "failed"
        )
    
    # Can go queued -> running -> completed
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
    
    # Can't go from completed back to running (terminal state)
    with pytest.raises(ValueError, match="already in terminal status"):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            "profile-1", "session-1",
            "running"
        )


def test_update_batch_member_rejects_unknown_member(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # All completed -> completed
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Two completed, one failed -> partial
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Both failed -> failed
    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "failed",
            error=f"error-{i}"
        )
    
    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "failed"


def test_finalized_batch_rejects_further_updates(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    for i in range(2):
        batch_jobs.update_batch_member(
            root, DATE, BATCH_ID,
            f"profile-{i}", f"session-{i}",
            "completed",
            version_id=f"ver-{i}"
        )
    batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    
    # Can't update finalized batch
    with pytest.raises(ValueError, match="already finalized"):
        batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    
    # But update_batch_member for an existing member should also fail after finalization
    # since members are terminal


# ---------------------------------------------------------------------
# Error sanitation
# ---------------------------------------------------------------------

def test_error_sanitizes_control_characters(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
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
    
    # Create one valid batch
    batch_jobs.create_batch_job(root, DATE, "valid-batch", members)
    
    # Create malformed file
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Two completed, two skipped_current
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
    
    assert batch["completed"] == 0  # counts only after updates
    finalized = batch_jobs.finalize_batch_job(root, DATE, BATCH_ID)
    assert finalized["status"] == "completed"  # skips allowed
    assert finalized["skipped"] == 2


def test_skipped_running_allowed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
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
    assert finalized["status"] == "partial"  # one completed, one failed


# ---------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------

def test_recover_stale_batch_jobs_marks_failed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    batch_jobs.start_batch_job(root, DATE, BATCH_ID)
    
    # Simulate stale: manually update status to running without started_at
    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    data = json.loads(path.read_text())
    data["status"] = "running"
    data["started_at"] = data["created_at"]
    # Leave members as running/queued
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
    assert "stale" in (loaded["members"][0]["error"] or "").lower()
    assert "stale" in (loaded["members"][1]["error"] or "").lower()


def test_recover_stale_batch_jobs_ignores_terminal_batches(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(2)
    
    # Create completed batch
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
    
    # Recover shouldn't touch it
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
    batch = batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    # Reload from disk
    reloaded = batch_jobs.load_batch_job(root, DATE, BATCH_ID)
    assert reloaded is not None
    assert reloaded["batch_id"] == batch["batch_id"]
    assert reloaded["members"][0]["profile"] == "profile-0"


def test_atomic_write_preserves_permissions(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    members = make_members(1)
    batch_jobs.create_batch_job(root, DATE, BATCH_ID, members)
    
    path = root / "batch-jobs" / DATE / f"{BATCH_ID}.json"
    mode = path.stat().st_mode & 0o777
    # Should be 0o600 (user read/write only)
    assert mode == 0o600
