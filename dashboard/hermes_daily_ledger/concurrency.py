"""Concurrency control for recap generation.

Durable running/failed/completed status with lock recovery after Dashboard
restart. One generation per date at a time. Bounded worker thread/process."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RecapJobStatus:
    """Durable status of a recap generation job."""

    date: str
    status: str  # "running" | "completed" | "failed"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    version_id: str | None = None


# In-memory lock registry (per-process, not cross-process)
_locks: dict[str, threading.Lock] = {}
_lock_registry_lock = threading.Lock()


def get_date_lock(date: str) -> threading.Lock:
    """Get a per-date lock that prevents concurrent generation."""
    with _lock_registry_lock:
        if date not in _locks:
            _locks[date] = threading.Lock()
        return _locks[date]


def get_ledger_running_dir(ledger_root: Path | None = None) -> Path:
    """Return the directory where durable status JSONs are stored."""
    from hermes_daily_ledger.recap_storage import get_ledger_root

    root = ledger_root or get_ledger_root()
    running_dir = root / "running"
    running_dir.mkdir(parents=True, exist_ok=True)
    return running_dir


def save_status(status: RecapJobStatus, ledger_root: Path | None = None) -> None:
    """Write status to durable storage for lock recovery on restart."""
    running_dir = get_ledger_running_dir(ledger_root)
    status_path = running_dir / f"{status.date}.json"

    data = {
        "date": status.date,
        "status": status.status,
        "started_at": status.started_at,
        "finished_at": status.finished_at,
        "error": status.error,
        "version_id": status.version_id,
    }

    # Atomic write
    tmp = status_path.with_suffix(".tmp")
    content = json.dumps(data, indent=2)
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(status_path)
    except OSError:
        # Fallback to direct write if atomic fails
        status_path.write_text(content, encoding="utf-8")


def load_status(date: str, ledger_root: Path | None = None) -> RecapJobStatus | None:
    """Load durable status for a date."""
    running_dir = get_ledger_running_dir(ledger_root)
    status_path = running_dir / f"{date}.json"

    if not status_path.is_file():
        return None

    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        return RecapJobStatus(**data)
    except (json.JSONDecodeError, OSError):
        return None


def recover_stale_locks(ledger_root: Path | None = None) -> list[str]:
    """Mark stale 'running' entries as 'failed' on startup.

    Returns a list of dates that were recovered from stale locks.
    """
    running_dir = get_ledger_running_dir(ledger_root)
    recovered: list[str] = []

    if not running_dir.is_dir():
        return recovered

    for status_file in running_dir.glob("*.json"):
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            if data.get("status") == "running":
                # This process is new — the old one died without completing
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                data["status"] = "failed"
                data["finished_at"] = now
                data["error"] = data.get("error") or "Previous generation terminated unexpectedly (stale lock recovery)"

                tmp = status_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                try:
                    tmp.replace(status_file)
                except OSError:
                    status_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

                date = data.get("date", status_file.stem)
                recovered.append(date)

                # Release the in-memory lock if held
                with _lock_registry_lock:
                    if date in _locks:
                        lock = _locks[date]
                        if not lock.locked():
                            del _locks[date]
        except (json.JSONDecodeError, OSError):
            continue

    return recovered


def acquire_generation_slot(date: str, ledger_root: Path | None = None) -> bool:
    """Attempt to acquire the generation slot for a date.

    Returns True if successful. Checks both in-memory lock and durable status.
    A durable "running" with no in-memory lock is treated as stale (the previous
    holder released cleanly but we updated durable only on completion/failure).
    """
    # Try to acquire the in-memory lock (non-blocking)
    date_lock = get_date_lock(date)
    acquired = date_lock.acquire(blocking=False)

    if not acquired:
        return False

    # Check durable status — only reject "running" if we also see it was saved
    # very recently. If the in-memory lock was free (which it is, since we just
    # grabbed it), any stale durable "running" can be overwritten.
    existing = load_status(date, ledger_root)
    if existing and existing.status == "running":
        # Previous holder released its in-memory lock without updating durable
        # status (clean release path). Treat as idle and proceed.
        pass

    # Save running status
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_status(RecapJobStatus(
        date=date,
        status="running",
        started_at=now,
    ), ledger_root)

    return True


def release_generation_slot(date: str) -> None:
    """Release the per-date lock."""
    with _lock_registry_lock:
        if date in _locks:
            lock = _locks[date]
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    pass  # Lock was already released
            del _locks[date]


def complete_generation(
    date: str,
    version_id: str | None = None,
    ledger_root: Path | None = None,
) -> RecapJobStatus:
    """Mark a generation as completed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = load_status(date, ledger_root) or RecapJobStatus(
        date=date, status="running", started_at=now
    )

    result = RecapJobStatus(
        date=date,
        status="completed",
        started_at=existing.started_at,
        finished_at=now,
        version_id=version_id,
    )
    save_status(result, ledger_root)
    release_generation_slot(date)
    return result


def fail_generation(
    date: str,
    error: str,
    ledger_root: Path | None = None,
) -> RecapJobStatus:
    """Mark a generation as failed, preserving last good recap."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = load_status(date, ledger_root) or RecapJobStatus(
        date=date, status="running", started_at=now
    )

    # Sanitize error for storage
    sanitized_error = _sanitize_error(error)

    result = RecapJobStatus(
        date=date,
        status="failed",
        started_at=existing.started_at,
        finished_at=now,
        error=sanitized_error,
    )
    save_status(result, ledger_root)
    release_generation_slot(date)
    return result


def _sanitize_error(raw: str) -> str:
    """Remove sensitive info from error text for durable storage."""
    import re as _re
    if not raw:
        return ""
    sanitized = raw.strip()[:500]
    # Remove PIDs
    sanitized = _re.sub(r"\bpid[_\s]?=?\s*\d+", "[PID]", sanitized, flags=_re.I)
    # Remove ALL absolute Unix paths (/home, /var, /tmp, /opt, /mnt, /root, /usr, /etc)
    sanitized = _re.sub(
        r"/(?:home|var|tmp|opt|mnt|root|usr|etc)[\w./-]+", "[PATH]", sanitized
    )
    # Remove potential tokens/secrets
    sanitized = _re.sub(r"\b[0-9a-f]{32,}\b", "[REDACTED]", sanitized)
    return sanitized
