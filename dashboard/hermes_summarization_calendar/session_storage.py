"""Immutable storage for per-session summaries and summary-only roll-ups.

The legacy whole-day recap layout remains in :mod:`recap_storage`.  This module
uses separate paths and hashed composite identities so profile/session strings
never become filesystem components.  Stored ``raw.json`` means raw *validated
summary output*; transcript/message payloads are rejected.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .recap_storage import (
    _atomic_write_json,
    _atomic_write_text,
    _version_timestamp,
    get_ledger_root,
    validate_version_id,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DISALLOWED_RAW_KEYS = {
    "messages",
    "message",
    "transcript",
    "transcripts",
    "raw_messages",
    "system_prompt",
    "tool_calls",
}


@dataclass(frozen=True)
class SummaryVersion:
    """Metadata for one immutable session-summary or roll-up version."""

    artifact_kind: str
    date: str
    version_id: str
    generated_at: str
    source_fingerprint: str
    artifact_key: str
    profile: str = ""
    session_id: str = ""
    title: str = ""


def _validate_date(date: str) -> str:
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        raise ValueError("date must use strict YYYY-MM-DD format")
    try:
        parsed = datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be a real calendar date") from exc
    if parsed.strftime("%Y-%m-%d") != date:
        raise ValueError("date must use strict YYYY-MM-DD format")
    return date


def artifact_key(date: str, profile: str, session_id: str) -> str:
    """Return a stable path-safe key for a canonical composite identity."""
    _validate_date(date)
    if not isinstance(profile, str) or not profile:
        raise ValueError("profile must be a non-empty string")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    identity = "\0".join((date, profile, session_id)).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def _new_version_id() -> str:
    return _version_timestamp()


def _ensure_private_dir(root: Path, target: Path) -> None:
    """Create a directory chain below *root* without following symlinks."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise OSError(f"Unsafe ledger root: {root}")
    try:
        root.chmod(0o700)
    except OSError:
        pass

    relative = target.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise OSError(f"Unsafe directory component: {current}")
        else:
            current.mkdir(mode=0o700)
        try:
            current.chmod(0o700)
        except OSError:
            pass


def _is_safe_existing_dir_chain(root: Path, target: Path) -> bool:
    """Return whether every existing directory from *root* to *target* is real.

    Read and rollback paths must not call the creating helper above, but they
    still need to reject a symlink in any parent component.
    """
    if root.is_symlink() or not root.is_dir():
        return False
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def _paths(
    root: Path,
    kind: str,
    date: str,
    key: str,
) -> tuple[Path, Path]:
    if kind == "session-summary":
        return (
            root / "session-summaries" / date / key,
            root / "session-versions" / date / key,
        )
    if kind == "rollup":
        return root / "rollups" / date, root / "rollup-versions" / date
    raise ValueError(f"Unknown artifact kind: {kind}")


@contextmanager
def _artifact_lock(root: Path, kind: str, key: str) -> Iterator[None]:
    lock_dir = root / ".locks"
    _ensure_private_dir(root, lock_dir)
    lock_path = lock_dir / f"{kind}-{key}.lock"
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


def _install_strict_current_pointer(current: Path, temporary: Path) -> None:
    """Install only a symlink; never migrate or delete a real directory."""
    if not temporary.is_symlink():
        raise OSError(f"Temporary pointer is not a symlink: {temporary}")
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise OSError(f"Current artifact pointer is not a symlink: {current}")
    os.replace(temporary, current)
    directory_fd = os.open(
        current.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _reject_raw_transcript_shapes(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in _DISALLOWED_RAW_KEYS:
                raise ValueError(f"raw transcript field is forbidden: {key}")
            _reject_raw_transcript_shapes(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_raw_transcript_shapes(nested)


def _render_markdown(kind: str, data: dict[str, Any], meta: dict[str, Any]) -> str:
    if kind == "session-summary":
        lines = [
            f"# {meta['title']}",
            "",
            f"- Date: {meta['date']}",
            f"- Profile: {meta['profile']}",
            f"- Session: {meta['session_id']}",
            f"- Generated: {meta['generated_at']}",
            "",
            str(data.get("summary", "")).strip(),
        ]
        key_points = data.get("key_points")
        if isinstance(key_points, list) and key_points:
            lines.extend(["", "## Key points", ""])
            lines.extend(f"- {str(point).strip()}" for point in key_points if str(point).strip())
        return "\n".join(lines).rstrip() + "\n"

    raw_coverage = data.get("coverage")
    coverage: dict[str, Any] = raw_coverage if isinstance(raw_coverage, dict) else {}
    return "\n".join(
        [
            f"# Summarization Calendar Roll-up — {meta['date']}",
            "",
            f"- Generated: {meta['generated_at']}",
            f"- Coverage: {coverage.get('included', 0)} of {coverage.get('active', 0)} sessions",
            "",
            str(data.get("overall_recap", "")).strip(),
            "",
        ]
    )


def _save(
    *,
    kind: str,
    date: str,
    key: str,
    data: dict[str, Any],
    source_fingerprint: str,
    generated_at: str | None,
    collection_cutoff_utc: str,
    model_profile: str,
    model: str,
    profile: str,
    session_id: str,
    title: str,
    ledger_root: Path | None,
) -> SummaryVersion:
    _validate_date(date)
    if not isinstance(data, dict):
        raise ValueError("summary data must be an object")
    _reject_raw_transcript_shapes(data)
    if not isinstance(source_fingerprint, str) or not source_fingerprint:
        raise ValueError("source_fingerprint must be non-empty")

    root = ledger_root or get_ledger_root()
    current, version_parent = _paths(root, kind, date, key)
    _ensure_private_dir(root, current.parent)
    _ensure_private_dir(root, version_parent)

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _artifact_lock(root, kind, key):
        version_id = _new_version_id()
        if not validate_version_id(version_id):
            raise ValueError("generated unsafe version ID")
        version_dir = version_parent / version_id
        version_dir.mkdir(mode=0o700, exist_ok=False)
        version_dir.chmod(0o700)

        meta: dict[str, Any] = {
            "artifact_kind": kind,
            "artifact_key": key,
            "date": date,
            "generated_at": generated_at,
            "collection_cutoff_utc": collection_cutoff_utc,
            "model_profile": model_profile,
            "model": model,
            "source_fingerprint": source_fingerprint,
            "version_id": version_id,
        }
        if kind == "session-summary":
            meta.update({
                "profile": profile,
                "session_id": session_id,
                "title": title,
            })

        try:
            _atomic_write_json(version_dir / "meta.json", meta)
            _atomic_write_json(version_dir / "raw.json", data)
            _atomic_write_text(version_dir / "summary.md", _render_markdown(kind, data, meta))
        except BaseException:
            shutil.rmtree(version_dir, ignore_errors=True)
            raise

        relative_target = os.path.relpath(version_dir, current.parent)
        temporary = current.with_name(
            f".{current.name}.tmp-link-{version_id}-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            os.symlink(relative_target, temporary)
            _install_strict_current_pointer(current, temporary)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    return SummaryVersion(
        artifact_kind=kind,
        date=date,
        version_id=version_id,
        generated_at=generated_at,
        source_fingerprint=source_fingerprint,
        artifact_key=key,
        profile=profile,
        session_id=session_id,
        title=title,
    )


def save_session_summary(
    date: str,
    profile: str,
    session_id: str,
    title: str,
    data: dict[str, Any],
    source_fingerprint: str,
    *,
    generated_at: str | None = None,
    collection_cutoff_utc: str = "",
    model_profile: str = "auxiliary.compression",
    model: str = "",
    ledger_root: Path | None = None,
) -> SummaryVersion:
    key = artifact_key(date, profile, session_id)
    return _save(
        kind="session-summary",
        date=date,
        key=key,
        data=data,
        source_fingerprint=source_fingerprint,
        generated_at=generated_at,
        collection_cutoff_utc=collection_cutoff_utc,
        model_profile=model_profile,
        model=model,
        profile=profile,
        session_id=session_id,
        title=title,
        ledger_root=ledger_root,
    )


def save_rollup(
    date: str,
    data: dict[str, Any],
    source_fingerprint: str,
    *,
    generated_at: str | None = None,
    collection_cutoff_utc: str = "",
    model_profile: str = "auxiliary.compression",
    model: str = "",
    ledger_root: Path | None = None,
) -> SummaryVersion:
    _validate_date(date)
    return _save(
        kind="rollup",
        date=date,
        key=date,
        data=data,
        source_fingerprint=source_fingerprint,
        generated_at=generated_at,
        collection_cutoff_utc=collection_cutoff_utc,
        model_profile=model_profile,
        model=model,
        profile="",
        session_id="",
        title="",
        ledger_root=ledger_root,
    )


def _resolve_current(
    root: Path,
    kind: str,
    date: str,
    key: str,
) -> Path | None:
    current, version_parent = _paths(root, kind, date, key)
    if not _is_safe_existing_dir_chain(root, current.parent):
        return None
    if not _is_safe_existing_dir_chain(root, version_parent):
        return None
    if not current.is_symlink():
        return None
    try:
        target = current.resolve(strict=True)
        expected_parent = version_parent.resolve(strict=True)
        target.relative_to(expected_parent)
    except (OSError, RuntimeError, ValueError):
        return None
    if target.parent != expected_parent or not target.is_dir() or target.is_symlink():
        return None
    return target


def _expected_meta(
    kind: str,
    date: str,
    key: str,
    profile: str,
    session_id: str,
) -> dict[str, str]:
    expected = {"artifact_kind": kind, "artifact_key": key, "date": date}
    if kind == "session-summary":
        expected.update({"profile": profile, "session_id": session_id})
    return expected


def _read_complete(
    directory: Path,
    expected: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if directory.is_symlink() or not directory.is_dir():
        return None, None
    try:
        for filename in ("meta.json", "raw.json", "summary.md"):
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                return None, None
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        raw = json.loads((directory / "raw.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(meta, dict) or not isinstance(raw, dict):
        return None, None
    if any(meta.get(field) != value for field, value in expected.items()):
        return None, None
    if not validate_version_id(str(meta.get("version_id", ""))):
        return None, None
    try:
        _reject_raw_transcript_shapes(raw)
    except ValueError:
        return None, None
    return raw, meta


def _load(
    kind: str,
    date: str,
    key: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _validate_date(date)
    root = ledger_root or get_ledger_root()
    directory = _resolve_current(root, kind, date, key)
    if directory is None:
        return None, None
    return _read_complete(
        directory,
        _expected_meta(kind, date, key, profile, session_id),
    )


def load_session_summary(
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    key = artifact_key(date, profile, session_id)
    return _load("session-summary", date, key, profile, session_id, ledger_root)


def load_rollup(
    date: str,
    ledger_root: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _validate_date(date)
    return _load("rollup", date, date, "", "", ledger_root)


def session_summary_exists(
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None = None,
) -> bool:
    raw, meta = load_session_summary(date, profile, session_id, ledger_root)
    return raw is not None and meta is not None


def check_session_staleness(
    date: str,
    profile: str,
    session_id: str,
    current_fingerprint: str,
    ledger_root: Path | None = None,
) -> bool:
    _, meta = load_session_summary(date, profile, session_id, ledger_root)
    if meta is None:
        return False
    return meta.get("source_fingerprint") != current_fingerprint


def _version_from_meta(meta: dict[str, Any]) -> SummaryVersion:
    return SummaryVersion(
        artifact_kind=str(meta.get("artifact_kind", "")),
        date=str(meta.get("date", "")),
        version_id=str(meta.get("version_id", "")),
        generated_at=str(meta.get("generated_at", "")),
        source_fingerprint=str(meta.get("source_fingerprint", "")),
        artifact_key=str(meta.get("artifact_key", "")),
        profile=str(meta.get("profile", "")),
        session_id=str(meta.get("session_id", "")),
        title=str(meta.get("title", "")),
    )


def _list_versions(
    kind: str,
    date: str,
    key: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None,
) -> list[SummaryVersion]:
    _validate_date(date)
    root = ledger_root or get_ledger_root()
    _, version_parent = _paths(root, kind, date, key)
    if not _is_safe_existing_dir_chain(root, version_parent):
        return []
    expected = _expected_meta(kind, date, key, profile, session_id)
    versions: list[SummaryVersion] = []
    for directory in sorted(version_parent.iterdir(), reverse=True):
        if not validate_version_id(directory.name):
            continue
        _, meta = _read_complete(directory, expected)
        if meta is not None and meta.get("version_id") == directory.name:
            versions.append(_version_from_meta(meta))
    return versions


def list_session_versions(
    date: str,
    profile: str,
    session_id: str,
    ledger_root: Path | None = None,
) -> list[SummaryVersion]:
    key = artifact_key(date, profile, session_id)
    return _list_versions(
        "session-summary", date, key, profile, session_id, ledger_root
    )


def list_rollup_versions(
    date: str,
    ledger_root: Path | None = None,
) -> list[SummaryVersion]:
    _validate_date(date)
    return _list_versions("rollup", date, date, "", "", ledger_root)


def _rollback(
    kind: str,
    date: str,
    key: str,
    profile: str,
    session_id: str,
    version_id: str,
    ledger_root: Path | None,
) -> SummaryVersion | None:
    _validate_date(date)
    if not validate_version_id(version_id):
        return None
    root = ledger_root or get_ledger_root()
    current, version_parent = _paths(root, kind, date, key)
    if not _is_safe_existing_dir_chain(root, version_parent):
        return None
    source = version_parent / version_id
    expected = _expected_meta(kind, date, key, profile, session_id)
    _, meta = _read_complete(source, expected)
    if meta is None or meta.get("version_id") != version_id:
        return None

    _ensure_private_dir(root, current.parent)
    with _artifact_lock(root, kind, key):
        relative_target = os.path.relpath(source, current.parent)
        temporary = current.with_name(
            f".{current.name}.tmp-rollback-{version_id}-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            os.symlink(relative_target, temporary)
            _install_strict_current_pointer(current, temporary)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return _version_from_meta(meta)


def rollback_session_summary(
    date: str,
    profile: str,
    session_id: str,
    version_id: str,
    ledger_root: Path | None = None,
) -> SummaryVersion | None:
    key = artifact_key(date, profile, session_id)
    return _rollback(
        "session-summary",
        date,
        key,
        profile,
        session_id,
        version_id,
        ledger_root,
    )


def rollback_rollup(
    date: str,
    version_id: str,
    ledger_root: Path | None = None,
) -> SummaryVersion | None:
    _validate_date(date)
    return _rollback("rollup", date, date, "", "", version_id, ledger_root)
