"""Keyed durable job control for session summaries and daily roll-ups."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .concurrency import _sanitize_error
from .recap_storage import _atomic_write_json, get_ledger_root
from .session_storage import _ensure_private_dir, _validate_date, artifact_key

_MAX_ACTIVE = 4
_registry_lock = threading.Lock()
_active_jobs: set[str] = set()


@dataclass(frozen=True)
class SummaryJobStatus:
    kind: str
    date: str
    status: str
    profile: str = ""
    session_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    version_id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root(ledger_root: Path | None) -> Path:
    return ledger_root or get_ledger_root()


def _session_key(date: str, profile: str, session_id: str) -> str:
    return artifact_key(date, profile, session_id)


def _rollup_key(date: str) -> str:
    _validate_date(date)
    return date


def _registry_key(kind: str, key: str) -> str:
    return f"{kind}:{key}"


def _status_path(
    kind: str,
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None,
) -> Path:
    root = _root(ledger_root)
    if kind == "session-summary":
        key = _session_key(date, profile, session_id)
        return root / "running" / "sessions" / f"{key}.json"
    if kind == "rollup":
        return root / "running" / "rollups" / f"{_rollup_key(date)}.json"
    raise ValueError(f"unknown job kind: {kind}")


def _save(status: SummaryJobStatus, ledger_root: Path | None) -> None:
    path = _status_path(
        status.kind,
        status.date,
        status.profile,
        status.session_id,
        ledger_root,
    )
    root = _root(ledger_root)
    _ensure_private_dir(root, path.parent)
    _atomic_write_json(path, asdict(status))


def _load(
    kind: str,
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None,
) -> SummaryJobStatus | None:
    path = _status_path(kind, date, profile, session_id, ledger_root)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        status = SummaryJobStatus(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if (
        status.kind != kind
        or status.date != date
        or status.profile != profile
        or status.session_id != session_id
        or status.status not in {"running", "completed", "failed"}
    ):
        return None
    return status


def _acquire(
    kind: str,
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None,
) -> SummaryJobStatus | None:
    key = (
        _session_key(date, profile, session_id)
        if kind == "session-summary"
        else _rollup_key(date)
    )
    registry_key = _registry_key(kind, key)
    with _registry_lock:
        if registry_key in _active_jobs or len(_active_jobs) >= _MAX_ACTIVE:
            return None
        _active_jobs.add(registry_key)

    status = SummaryJobStatus(
        kind=kind,
        date=date,
        profile=profile,
        session_id=session_id,
        status="running",
        started_at=_now(),
    )
    try:
        _save(status, ledger_root)
    except BaseException:
        _release(kind, key)
        raise
    return status


def _release(kind: str, key: str) -> None:
    with _registry_lock:
        _active_jobs.discard(_registry_key(kind, key))


def acquire_session_job(
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus | None:
    return _acquire("session-summary", date, profile, session_id, ledger_root)


def acquire_rollup_job(
    date: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus | None:
    return _acquire("rollup", date, "", "", ledger_root)


def load_session_job(
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus | None:
    return _load("session-summary", date, profile, session_id, ledger_root)


def load_rollup_job(
    date: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus | None:
    return _load("rollup", date, "", "", ledger_root)


def _finish(
    kind: str,
    date: str,
    profile: str,
    session_id: str,
    *,
    version_id: str | None,
    error: str | None,
    ledger_root: Path | None,
) -> SummaryJobStatus:
    existing = _load(kind, date, profile, session_id, ledger_root)
    status = SummaryJobStatus(
        kind=kind,
        date=date,
        profile=profile,
        session_id=session_id,
        status="failed" if error is not None else "completed",
        started_at=existing.started_at if existing else _now(),
        finished_at=_now(),
        error=_sanitize_error(error or "") if error is not None else None,
        version_id=version_id if error is None else None,
    )
    key = (
        _session_key(date, profile, session_id)
        if kind == "session-summary"
        else _rollup_key(date)
    )
    try:
        _save(status, ledger_root)
    finally:
        _release(kind, key)
    return status


def complete_session_job(
    date: str,
    profile: str,
    session_id: str,
    version_id: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus:
    return _finish(
        "session-summary", date, profile, session_id,
        version_id=version_id, error=None, ledger_root=ledger_root,
    )


def fail_session_job(
    date: str,
    profile: str,
    session_id: str,
    error: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus:
    return _finish(
        "session-summary", date, profile, session_id,
        version_id=None, error=error, ledger_root=ledger_root,
    )


def complete_rollup_job(
    date: str,
    version_id: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus:
    return _finish(
        "rollup", date, "", "", version_id=version_id, error=None,
        ledger_root=ledger_root,
    )


def fail_rollup_job(
    date: str,
    error: str,
    ledger_root: Path | None = None,
) -> SummaryJobStatus:
    return _finish(
        "rollup", date, "", "", version_id=None, error=error,
        ledger_root=ledger_root,
    )


def recover_stale_jobs(ledger_root: Path | None = None) -> list[str]:
    root = _root(ledger_root)
    recovered: list[str] = []
    for directory in (root / "running" / "sessions", root / "running" / "rollups"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                status = SummaryJobStatus(**data)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if status.status != "running":
                continue
            failed = SummaryJobStatus(
                kind=status.kind,
                date=status.date,
                profile=status.profile,
                session_id=status.session_id,
                status="failed",
                started_at=status.started_at,
                finished_at=_now(),
                error="Previous generation terminated unexpectedly (stale job recovery)",
            )
            try:
                _save(failed, root)
            except (OSError, ValueError):
                continue
            recovered.append(
                f"{status.kind}:{status.date}:{status.profile}:{status.session_id}"
            )
    return recovered


def _reset_for_tests() -> None:
    with _registry_lock:
        _active_jobs.clear()
