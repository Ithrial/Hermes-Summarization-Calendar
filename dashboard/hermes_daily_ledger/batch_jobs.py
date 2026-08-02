"""Durable batch job storage for session summaries and daily roll-ups.

Provides atomic batch lifecycle management with per-member tracking.
Batch status is persisted at `<ledger_root>/batch-jobs/<YYYY-MM-DD>/<batch-id>.json`.

Member status values:
    queued | running | completed | failed | skipped_current | skipped_running

Batch status derivation:
    - completed: no member failed (skips allowed)
    - partial: at least one completed AND at least one failed
    - failed: at least one failed AND zero completed
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_VALID_MEMBER_STATUSES = frozenset({
    "queued", "running", "completed", "failed", "skipped_current", "skipped_running"
})
_VALID_BATCH_STATUSES = frozenset({"queued", "running", "completed", "partial", "failed"})

_MAX_MEMBER_COUNT = 100
_MIN_MEMBER_COUNT = 1
_MAX_ERROR_LENGTH = 500


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_error(raw: str) -> str:
    """Remove control characters and cap length for durable storage."""
    if not isinstance(raw, str):
        return ""
    sanitized = raw.strip()
    # Remove control characters except common whitespace
    sanitized = "".join(
        ch for ch in sanitized
        if ch == "\n" or ch == "\t" or (" " <= ch < "\x7f")
    )
    return sanitized[:_MAX_ERROR_LENGTH]


def _validate_date(date_str: str) -> str:
    """Validate strict YYYY-MM-DD format."""
    if not isinstance(date_str, str) or not _DATE_RE.fullmatch(date_str):
        raise ValueError("date must use strict YYYY-MM-DD format")
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be a real calendar date") from exc
    if parsed.strftime("%Y-%m-%d") != date_str:
        raise ValueError("date must use strict YYYY-MM-DD format")
    return date_str


def _validate_batch_id(batch_id: str) -> str:
    """Validate batch ID is path-safe alphanumeric with underscores/hyphens."""
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("batch_id must be a non-empty string")
    if not _BATCH_ID_RE.fullmatch(batch_id):
        raise ValueError("batch_id must contain only letters, digits, underscores, or hyphens")
    return batch_id


def _validate_members(members: list[dict[str, Any]]) -> None:
    """Validate members list meets all constraints."""
    if not isinstance(members, list):
        raise ValueError("members must be a list")
    if len(members) < _MIN_MEMBER_COUNT or len(members) > _MAX_MEMBER_COUNT:
        raise ValueError(f"members count must be between {_MIN_MEMBER_COUNT} and {_MAX_MEMBER_COUNT}")
    
    seen = set()
    for idx, member in enumerate(members):
        if not isinstance(member, dict):
            raise ValueError(f"member at index {idx} must be a dict")
        if "profile" not in member or "session_id" not in member:
            raise ValueError(f"member at index {idx} must have 'profile' and 'session_id'")
        profile = member["profile"]
        session_id = member["session_id"]
        if not isinstance(profile, str) or not profile:
            raise ValueError(f"member at index {idx} profile must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"member at index {idx} session_id must be a non-empty string")
        composite = f"{profile}\0{session_id}"
        if composite in seen:
            raise ValueError(f"member at index {idx} has duplicate composite identity")
        seen.add(composite)


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with fsync and restrictive permissions."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(parent),
            suffix=".tmp",
            prefix=".batch_",
        )
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_json_file(path: Path) -> dict[str, Any] | None:
    """Load JSON file safely, returning None on any failure."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _safe_date_path(ledger_root: Path, date_str: str) -> Path:
    """Build a safe batch job path for a date, never interpolating untrusted data."""
    _validate_date(date_str)
    path = ledger_root / "batch-jobs" / date_str
    if path.is_symlink():
        raise OSError(f"Unsafe path component in ledger_root: {ledger_root}")
    return path


def create_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
    members: list[dict[str, Any]],
    regenerate_current: bool = False,
) -> dict[str, Any]:
    """Create a new batch job with initial queued status.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        Unique identifier for this batch (alphanumeric with underscores/hyphens).
    members : list[dict]
        List of member dicts, each with 'profile' and 'session_id' keys.
    regenerate_current : bool
        Whether this batch is regenerating current summaries.
    
    Returns
    -------
    dict
        The created batch job status object.
    
    Raises
    ------
    ValueError
        If date, batch_id, or members are invalid.
    OSError
        If path traversal is detected or atomic write fails.
    """
    _validate_date(date_str)
    _validate_batch_id(batch_id)
    _validate_members(members)
    
    root = ledger_root
    date_path = _safe_date_path(root, date_str)
    date_path.mkdir(parents=True, exist_ok=True)
    
    batch_path = date_path / f"{batch_id}.json"
    if batch_path.exists() or batch_path.is_symlink():
        raise ValueError(f"batch job {batch_id} already exists for {date_str}")
    
    now = _now()
    composite_members = []
    for member in members:
        composite_members.append({
            "profile": member["profile"],
            "session_id": member["session_id"],
            "status": "queued",
            "error": None,
            "version_id": None,
        })
    
    batch_job = {
        "schema_version": 1,
        "batch_id": batch_id,
        "date": date_str,
        "regenerate_current": bool(regenerate_current),
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "total": len(members),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "current": None,
        "members": composite_members,
    }
    
    _atomic_write_json(batch_path, batch_job)
    return batch_job


def load_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
) -> dict[str, Any] | None:
    """Load a batch job by date and batch_id.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        The batch job identifier.
    
    Returns
    -------
    dict or None
        The batch job status object, or None if not found/invalid.
    """
    _validate_date(date_str)
    _validate_batch_id(batch_id)
    
    root = ledger_root
    batch_path = _safe_date_path(root, date_str) / f"{batch_id}.json"
    
    data = _load_json_file(batch_path)
    if data is None:
        return None
    
    # Validate required fields and schema
    required = {"schema_version", "batch_id", "date", "status", "members"}
    if not isinstance(data, dict) or not required.issubset(data.keys()):
        return None
    
    if data.get("schema_version") != 1:
        return None
    
    if data.get("date") != date_str or data.get("batch_id") != batch_id:
        return None
    
    if data.get("status") not in _VALID_BATCH_STATUSES:
        return None
    
    if not isinstance(data.get("members"), list):
        return None
    
    return data


def list_batch_jobs(
    ledger_root: Path,
    date_str: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List batch jobs for a date, newest first.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    limit : int
        Maximum number of jobs to return (positive, max unbounded but reasonable).
    
    Returns
    -------
    list[dict]
        List of batch job status objects, sorted newest first.
    """
    _validate_date(date_str)
    
    if not isinstance(limit, int) or limit <= 0:
        limit = 20
    
    root = ledger_root
    date_path = _safe_date_path(root, date_str)
    
    if date_path.is_symlink() or not date_path.is_dir():
        return []
    
    jobs: list[dict[str, Any]] = []
    for path in date_path.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        
        data = _load_json_file(path)
        if data is None:
            continue
        
        required = {"schema_version", "batch_id", "date", "status", "members"}
        if not isinstance(data, dict) or not required.issubset(data.keys()):
            continue
        
        if data.get("schema_version") != 1:
            continue
        
        if data.get("date") != date_str:
            continue
        
        jobs.append(data)
        
        if len(jobs) >= limit:
            break
    
    # Sort by created_at descending (newest first)
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    
    return jobs[:limit]


def start_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
) -> dict[str, Any]:
    """Mark a batch job as running and record start time.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        The batch job identifier.
    
    Returns
    -------
    dict
        The updated batch job status.
    
    Raises
    ------
    ValueError
        If batch not found or already started.
    """
    batch_job = load_batch_job(ledger_root, date_str, batch_id)
    if batch_job is None:
        raise ValueError(f"batch job {batch_id} not found for {date_str}")
    
    if batch_job["status"] != "queued":
        raise ValueError(f"batch job {batch_id} is not in queued status")
    
    now = _now()
    batch_job["status"] = "running"
    batch_job["started_at"] = now
    
    _save_batch_job(ledger_root, date_str, batch_id, batch_job)
    return batch_job


def update_batch_member(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
    profile: str,
    session_id: str,
    status: str,
    error: str | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Update status for a single batch member.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        The batch job identifier.
    profile : str
        Member profile name.
    session_id : str
        Member session_id.
    status : str
        New status: queued | running | completed | failed | skipped_current | skipped_running
    error : str or None
        Optional bounded sanitized error message.
    version_id : str or None
        Optional version_id for completed members.
    
    Returns
    -------
    dict
        The updated batch job status.
    
    Raises
    ------
    ValueError
        If batch not found, member not found, or status transition invalid.
    """
    _validate_batch_id(profile)
    _validate_batch_id(session_id)
    
    if status not in _VALID_MEMBER_STATUSES:
        raise ValueError(f"invalid member status: {status}")
    
    batch_job = load_batch_job(ledger_root, date_str, batch_id)
    if batch_job is None:
        raise ValueError(f"batch job {batch_id} not found for {date_str}")
    
    # Find and update member
    member_found = False
    current_member = None
    composite = f"{profile}\0{session_id}"
    
    for member in batch_job["members"]:
        if f"{member['profile']}\0{member['session_id']}" == composite:
            member_found = True
            current_member = member
            break
    
    if not member_found:
        raise ValueError(f"member {profile}/{session_id} not found in batch {batch_id}")
    
    # Validate transitions
    current_status = current_member["status"]
    if current_status == status:
        # Same status is allowed (e.g., re-entrancy for running)
        pass
    elif current_status == "queued" and status in {"running", "completed", "failed", "skipped_current", "skipped_running"}:
        # queued can transition to any status
        pass
    elif current_status == "running" and status in {"completed", "failed", "skipped_running"}:
        # running -> completed/failed/skipped_running is allowed
        pass
    elif current_status == "running" and status == "running":
        # running -> running (re-entrancy)
        pass
    elif current_status == "running" and status == "skipped_current":
        # running -> skipped_current is not allowed (should go to terminal status first)
        raise ValueError(f"invalid transition from running to {status}")
    elif current_status in {"completed", "failed", "skipped_current", "skipped_running"}:
        raise ValueError(f"member already in terminal status {current_status}")
    
    # Update member
    sanitized_error = _sanitize_error(error or "")
    current_member["status"] = status
    current_member["error"] = sanitized_error if sanitized_error else None
    if status in {"completed"}:
        current_member["version_id"] = version_id
    else:
        current_member["version_id"] = None
    
    # Recalculate counts
    total = len(batch_job["members"])
    completed = sum(1 for m in batch_job["members"] if m["status"] == "completed")
    failed = sum(1 for m in batch_job["members"] if m["status"] == "failed")
    skipped = sum(1 for m in batch_job["members"] if m["status"] in {"skipped_current", "skipped_running"})
    
    batch_job["completed"] = completed
    batch_job["failed"] = failed
    batch_job["skipped"] = skipped
    
    # Update current pointer if member is running
    if status == "running":
        batch_job["current"] = {"profile": profile, "session_id": session_id}
    
    _save_batch_job(ledger_root, date_str, batch_id, batch_job)
    return batch_job


def finalize_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
) -> dict[str, Any]:
    """Manually finalize a batch job, deriving completion status.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    date_str : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        The batch job identifier.
    
    Returns
    -------
    dict
        The finalized batch job status.
    
    Raises
    ------
    ValueError
        If batch not found or already finalized.
    """
    batch_job = load_batch_job(ledger_root, date_str, batch_id)
    if batch_job is None:
        raise ValueError(f"batch job {batch_id} not found for {date_str}")
    
    if batch_job["status"] in {"completed", "partial", "failed"}:
        raise ValueError(f"batch job {batch_id} is already finalized")
    
    completed = batch_job["completed"]
    failed = batch_job["failed"]
    skipped_running = sum(1 for m in batch_job["members"] if m["status"] == "skipped_running")
    skipped_current = sum(1 for m in batch_job["members"] if m["status"] == "skipped_current")
    
    # skipped_current doesn't count as failure; skipped_running does
    effective_failed = failed + skipped_running
    
    if effective_failed == 0:
        status = "completed"
    elif completed > 0 and effective_failed > 0:
        status = "partial"
    else:
        status = "failed"
    
    batch_job["status"] = status
    batch_job["finished_at"] = _now()
    
    _save_batch_job(ledger_root, date_str, batch_id, batch_job)
    return batch_job


def recover_stale_batch_jobs(
    ledger_root: Path,
) -> list[str]:
    """Mark queued/running batches and unfinished members as failed.
    
    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.
    
    Returns
    -------
    list[str]
        List of recovered batch IDs.
    """
    root = ledger_root
    batch_jobs_dir = root / "batch-jobs"
    
    if batch_jobs_dir.is_symlink() or not batch_jobs_dir.is_dir():
        return []
    
    recovered: list[str] = []
    now = _now()
    
    for date_dir in batch_jobs_dir.iterdir():
        if date_dir.is_symlink() or not date_dir.is_dir():
            continue
        
        date_str = date_dir.name
        if not _DATE_RE.fullmatch(date_str):
            continue
        
        for batch_file in date_dir.glob("*.json"):
            if batch_file.is_symlink() or not batch_file.is_file():
                continue
            
            data = _load_json_file(batch_file)
            if data is None:
                continue
            
            required = {"schema_version", "batch_id", "date", "status", "members"}
            if not isinstance(data, dict) or not required.issubset(data.keys()):
                continue
            
            if data.get("schema_version") != 1:
                continue
            
            batch_id = data.get("batch_id")
            if data.get("status") not in {"queued", "running"}:
                continue
            
            # Mark batch as failed
            data["status"] = "failed"
            data["finished_at"] = now
            
            # Mark unfinished members as failed
            for member in data["members"]:
                if member["status"] in {"queued", "running"}:
                    member["status"] = "failed"
                    member["error"] = "Batch process terminated unexpectedly (stale batch recovery)"
            
            _atomic_write_json(batch_file, data)
            recovered.append(f"{date_str}:{batch_id}")
    
    return recovered


def _save_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
    batch_job: dict[str, Any],
) -> None:
    """Atomically save batch job status."""
    root = ledger_root
    date_path = _safe_date_path(root, date_str)
    date_path.mkdir(parents=True, exist_ok=True)
    
    batch_path = date_path / f"{batch_id}.json"
    
    # Atomic write via temp + rename
    parent = batch_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    
    fd, tmp_path = tempfile.mkstemp(
        dir=str(parent),
        suffix=".tmp",
        prefix=".batch_",
    )
    try:
        content = json.dumps(batch_job, ensure_ascii=False, indent=2, sort_keys=True)
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    
    try:
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, str(batch_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
