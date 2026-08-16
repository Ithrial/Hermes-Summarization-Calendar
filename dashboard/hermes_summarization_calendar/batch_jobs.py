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

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_VALID_MEMBER_STATUSES = frozenset({
    "queued", "running", "completed", "failed", "skipped_current", "skipped_running"
})
_VALID_BATCH_STATUSES = frozenset({"queued", "running", "completed", "partial", "failed"})
_TERMINAL_MEMBER_STATUSES = frozenset({"completed", "failed", "skipped_current", "skipped_running"})

_MAX_MEMBER_COUNT = 100
_MIN_MEMBER_COUNT = 1
_MAX_ERROR_LENGTH = 500
_MAX_IDENTITY_LENGTH = 256


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_error(raw: str) -> str:
    """Remove control characters and cap length for durable storage."""
    if not isinstance(raw, str):
        return ""
    sanitized = raw.strip()
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


def _validate_identity(value: str, field_name: str) -> None:
    """Validate a profile or session_id identity value.

    Accepts real nonblank Hermes strings including dots/spaces/hyphens/underscores.
    Rejects non-string, blank/whitespace-only, NUL/control characters, unreasonable length.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {_MAX_IDENTITY_LENGTH}")
    for ch in value:
        if ch == "\0" or (ch < " " and ch not in ("\n", "\t")):
            raise ValueError(f"{field_name} must not contain NUL or control characters")


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
        _validate_identity(profile, f"member at index {idx} profile")
        _validate_identity(session_id, f"member at index {idx} session_id")
        composite = f"{profile}\0{session_id}"
        if composite in seen:
            raise ValueError(f"member at index {idx} has duplicate composite identity")
        seen.add(composite)


@contextmanager
def _batch_lock(root: Path, batch_key: str) -> Iterator[None]:
    """Acquire an exclusive fcntl.flock sidecar lock for a batch mutation.

    Follows the session_storage._artifact_lock convention.
    No nested reacquisition — callers must not hold this lock when calling another
    locked operation.
    """
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.chmod(0o700)
    except OSError:
        pass
    lock_path = lock_dir / f"batch-{batch_key}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Unsafe lock file: {lock_path}")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with fsync and restrictive permissions.

    This is the single atomic writer for batch jobs — private mode 0o600.
    """
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


def _batch_path(ledger_root: Path, date_str: str, batch_id: str) -> Path:
    """Resolve the canonical file path for a batch job."""
    return _safe_date_path(ledger_root, date_str) / f"{batch_id}.json"


def _is_valid_batch(data: dict[str, Any], date_str: str | None = None,
                    batch_id: str | None = None) -> bool:
    """Check whether loaded data is a valid batch job object."""
    required = {"schema_version", "batch_id", "date", "status", "members"}
    if not isinstance(data, dict) or not required.issubset(data.keys()):
        return False
    if data.get("schema_version") != 1:
        return False
    if data.get("status") not in _VALID_BATCH_STATUSES:
        return False
    if not isinstance(data.get("members"), list):
        return False
    if date_str is not None and data.get("date") != date_str:
        return False
    if batch_id is not None and data.get("batch_id") != batch_id:
        return False
    return True


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

    batch_file = _batch_path(root, date_str, batch_id)
    if batch_file.exists() or batch_file.is_symlink():
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

    _atomic_write_json(batch_file, batch_job)
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

    batch_file = _batch_path(ledger_root, date_str, batch_id)
    data = _load_json_file(batch_file)
    if data is None:
        return None
    if not _is_valid_batch(data, date_str, batch_id):
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
        Maximum number of jobs to return (1..20).

    Returns
    -------
    list[dict]
        List of batch job status objects, sorted newest first.
    """
    _validate_date(date_str)

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 20:
        raise ValueError("limit must be an integer between 1 and 20")

    date_path = _safe_date_path(ledger_root, date_str)
    if date_path.is_symlink() or not date_path.is_dir():
        return []

    jobs: list[dict[str, Any]] = []
    for path in date_path.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        data = _load_json_file(path)
        if data is None:
            continue
        if not _is_valid_batch(data, date_str):
            continue
        jobs.append(data)

    # Sort globally newest-first using created_at with deterministic tie-breaker
    jobs.sort(key=lambda x: (x.get("created_at", ""), x.get("batch_id", "")), reverse=True)

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
    with _batch_lock(ledger_root, f"{date_str}:{batch_id}"):
        batch_job = load_batch_job(ledger_root, date_str, batch_id)
        if batch_job is None:
            raise ValueError(f"batch job {batch_id} not found for {date_str}")

        if batch_job["status"] != "queued":
            raise ValueError(f"batch job {batch_id} is not in queued status")

        now = _now()
        batch_job["status"] = "running"
        batch_job["started_at"] = now

        batch_file = _batch_path(ledger_root, date_str, batch_id)
        _atomic_write_json(batch_file, batch_job)
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
        New member status.
    error : str or None
        Optional bounded sanitized error message (only retained for failed members).
    version_id : str or None
        Optional version_id (only retained for completed members).

    Returns
    -------
    dict
        The updated batch job status.

    Raises
    ------
    ValueError
        If batch not found, member not found, or status transition invalid.
    """
    _validate_identity(profile, "profile")
    _validate_identity(session_id, "session_id")

    if status not in _VALID_MEMBER_STATUSES:
        raise ValueError(f"invalid member status: {status}")

    with _batch_lock(ledger_root, f"{date_str}:{batch_id}"):
        batch_job = load_batch_job(ledger_root, date_str, batch_id)
        if batch_job is None:
            raise ValueError(f"batch job {batch_id} not found for {date_str}")

        # Find member by composite identity
        member_found = False
        current_member = None
        target_composite = f"{profile}\0{session_id}"

        for member in batch_job["members"]:
            if f"{member['profile']}\0{member['session_id']}" == target_composite:
                member_found = True
                current_member = member
                break

        if not member_found:
            raise ValueError(f"member {profile}/{session_id} not found in batch {batch_id}")

        # Validate transitions
        old_status = current_member["status"]
        if old_status == status:
            pass  # same status (re-entrancy)
        elif old_status == "queued" and status in _VALID_MEMBER_STATUSES - {"queued"}:
            pass  # queued -> any non-queued
        elif old_status == "running" and status in {"completed", "failed", "skipped_running"}:
            pass  # running -> terminal
        elif old_status in _TERMINAL_MEMBER_STATUSES:
            raise ValueError(f"member already in terminal status {old_status}")
        else:
            raise ValueError(f"invalid transition from {old_status} to {status}")

        # Update member fields
        current_member["status"] = status

        # Error only retained for failed members
        if status == "failed" and error is not None:
            current_member["error"] = _sanitize_error(error) or None
        else:
            current_member["error"] = None

        # version_id only retained for completed members
        if status == "completed":
            current_member["version_id"] = version_id
        else:
            current_member["version_id"] = None

        # Clear current pointer when the referenced running member becomes terminal
        old_current = batch_job.get("current")
        if (old_current is not None
                and old_current.get("profile") == profile
                and old_current.get("session_id") == session_id
                and status in _TERMINAL_MEMBER_STATUSES):
            batch_job["current"] = None

        # Set current pointer when member transitions to running
        if status == "running":
            batch_job["current"] = {"profile": profile, "session_id": session_id}

        # Recalculate counts from members
        completed = sum(1 for m in batch_job["members"] if m["status"] == "completed")
        failed = sum(1 for m in batch_job["members"] if m["status"] == "failed")
        skipped = sum(1 for m in batch_job["members"]
                      if m["status"] in ("skipped_current", "skipped_running"))

        batch_job["total"] = len(batch_job["members"])
        batch_job["completed"] = completed
        batch_job["failed"] = failed
        batch_job["skipped"] = skipped

        batch_file = _batch_path(ledger_root, date_str, batch_id)
        _atomic_write_json(batch_file, batch_job)
        return batch_job


def finalize_batch_job(
    ledger_root: Path,
    date_str: str,
    batch_id: str,
) -> dict[str, Any]:
    """Manually finalize a batch job, deriving completion status.

    Refuses to finalize while any member is still queued or running.

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
        If batch not found or members are still active. A terminal batch is
        returned unchanged so concurrent finalization is idempotent.
    """
    with _batch_lock(ledger_root, f"{date_str}:{batch_id}"):
        batch_job = load_batch_job(ledger_root, date_str, batch_id)
        if batch_job is None:
            raise ValueError(f"batch job {batch_id} not found for {date_str}")

        if batch_job["status"] in {"completed", "partial", "failed"}:
            # Finalization may race with the coordinator's own finally block.
            # Terminal state is immutable; return it rather than turning a
            # successful batch into a misleading worker error.
            return batch_job

        # Refuse to finalize while any member is queued/running
        active = [m for m in batch_job["members"] if m["status"] in ("queued", "running")]
        if active:
            raise ValueError(
                f"cannot finalize: {len(active)} member(s) still queued/running"
            )

        # Derive status from members
        completed = sum(1 for m in batch_job["members"] if m["status"] == "completed")
        failed = sum(1 for m in batch_job["members"] if m["status"] == "failed")

        if failed == 0:
            status = "completed"
        elif completed > 0:
            status = "partial"
        else:
            status = "failed"

        # Recalculate skipped from members for consistency
        skipped = sum(1 for m in batch_job["members"]
                      if m["status"] in ("skipped_current", "skipped_running"))
        batch_job["completed"] = completed
        batch_job["failed"] = failed
        batch_job["skipped"] = skipped

        batch_job["status"] = status
        batch_job["finished_at"] = _now()
        batch_job["current"] = None  # always clear on finalize

        batch_file = _batch_path(ledger_root, date_str, batch_id)
        _atomic_write_json(batch_file, batch_job)
        return batch_job


def recover_stale_batch_jobs(
    ledger_root: Path,
) -> list[str]:
    """Mark queued/running batches and unfinished members as failed.

    Refuses top-level batch-jobs symlink. Marks every queued/running member
    failed with a sanitized generic interrupted-process error, clears current,
    recalculates counts from members, sets failed + finished_at, preserves
    terminal members.

    Parameters
    ----------
    ledger_root : Path
        Root directory for ledger storage.

    Returns
    -------
    list[str]
        List of recovered batch identifiers as "date:batch_id".
    """
    root = ledger_root
    batch_jobs_dir = root / "batch-jobs"

    if batch_jobs_dir.is_symlink() or not batch_jobs_dir.is_dir():
        return []

    recovered: list[str] = []
    now = _now()
    recovery_error = "Batch process terminated unexpectedly (interrupted)"

    for date_dir in sorted(batch_jobs_dir.iterdir()):
        if date_dir.is_symlink() or not date_dir.is_dir():
            continue

        date_str = date_dir.name
        if not _DATE_RE.fullmatch(date_str):
            continue

        for batch_file in sorted(date_dir.glob("*.json")):
            if batch_file.is_symlink() or not batch_file.is_file():
                continue

            data = _load_json_file(batch_file)
            if data is None:
                continue

            if not _is_valid_batch(data):
                continue

            if data.get("status") not in {"queued", "running"}:
                continue

            batch_id = str(data["batch_id"])
            with _batch_lock(root, f"{date_str}:{batch_id}"):
                # Reload inside lock to avoid TOCTOU
                fresh = _load_json_file(batch_file)
                if fresh is None or not _is_valid_batch(fresh):
                    continue
                if fresh.get("status") in {"completed", "partial", "failed"}:
                    continue

                # Mark batch failed
                fresh["status"] = "failed"
                fresh["finished_at"] = now
                fresh["current"] = None  # clear current

                # Mark queued/running members as failed; preserve terminal members
                for member in fresh["members"]:
                    if member["status"] in ("queued", "running"):
                        member["status"] = "failed"
                        member["error"] = recovery_error
                        member["version_id"] = None

                # Recalculate counts from members
                completed = sum(1 for m in fresh["members"] if m["status"] == "completed")
                failed = sum(1 for m in fresh["members"] if m["status"] == "failed")
                skipped = sum(1 for m in fresh["members"]
                              if m["status"] in ("skipped_current", "skipped_running"))
                fresh["total"] = len(fresh["members"])
                fresh["completed"] = completed
                fresh["failed"] = failed
                fresh["skipped"] = skipped

                _atomic_write_json(batch_file, fresh)
                recovered.append(f"{date_str}:{batch_id}")

    return recovered
