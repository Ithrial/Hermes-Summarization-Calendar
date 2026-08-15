"""Atomic recap storage with immutable versioning.

Default root is ``~/.hermes/summarization-calendar`` (env override for tests).
Existing v1.1.0-and-earlier data under the legacy root
``~/.hermes/daily-ledger`` is followed in place when the new root has no
stored data yet (see :func:`get_ledger_root`), so pre-rename recaps remain
readable and no split-brain between the two roots occurs.
Each date has a current JSON+Markdown plus an immutable timestamped archive
when replaced. Uses atomic temp+fsync+replace so partial writes never
corrupt data. Restrictive user-only permissions (0o600/0o700).

Never stores secrets or raw transcripts — only validated, sanitized recaps.
Existing artifacts remain in place until successful replacement.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default ledger root — override via LEDGER_ROOT env in tests.
DEFAULT_LEDGER_ROOT = Path.home() / ".hermes" / "summarization-calendar"
# Legacy root used by v1.1.0-and-earlier installs.
LEGACY_LEDGER_ROOT = Path.home() / ".hermes" / "daily-ledger"

# Subdirectories that hold stored data. A root containing only an empty
# scaffold (created by _ensure_dirs) does NOT count as "having data".
_DATA_SUBDIRS = ("recaps", "versions", "session-versions", "rollup-versions")


@dataclass(frozen=True)
class RecapVersion:
    """Metadata for a single stored recap version."""

    date: str
    version_ts: str  # e.g. "20260701T183000Z"
    generated_at: str
    source_fingerprint: str
    session_count: int
    cron_count: int


def _root_has_data(root: Path) -> bool:
    """Return True if *root* contains at least one stored data artifact."""
    for name in _DATA_SUBDIRS:
        subdir = root / name
        if subdir.is_dir():
            try:
                if any(subdir.iterdir()):
                    return True
            except OSError:
                continue
    return False


def get_ledger_root() -> Path:
    """Return the ledger root directory, respecting LEDGER_ROOT env.

    Resolution order:
    1. ``LEDGER_ROOT`` env (tests / explicit override) — always wins.
    2. The new default root if it has stored data — a fresh install lands here.
    3. The legacy root (``~/.hermes/daily-ledger``) if *it* has stored data —
       an upgrade from v1.1.0-and-earlier keeps following the existing store
       in place so pre-rename recaps stay readable and no split-brain
       appears between the two roots. Data is never copied or moved.
    4. Otherwise the new default root.
    """
    env_val = os.environ.get("LEDGER_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    new_root = DEFAULT_LEDGER_ROOT
    if _root_has_data(new_root):
        return new_root.resolve()
    if _root_has_data(LEGACY_LEDGER_ROOT):
        return LEGACY_LEDGER_ROOT.resolve()
    return new_root.resolve()


# ---------------------------------------------------------------------------
# Version ID validation (shared between storage and API)
# ---------------------------------------------------------------------------

# Strict version ID pattern: YYYYMMDDTHHMMSSZ_microseconds_random
# Allows only alphanumerics, underscores, hyphens after the base timestamp.
# Rejects path separators and traversal sequences.
_VERSION_ID_RE = re.compile(
    r"^\d{8}T\d{6}Z_?[A-Za-z0-9_-]*$"
)


def validate_version_id(version_id: str) -> bool:
    """Validate a version ID is safe for use in filesystem paths and API.

    Returns True if the version ID matches our strict pattern:
    ``YYYYMMDDTHHMMSSZ`` optionally followed by ``_microseconds_random``.
    Rejects any path separators, traversal sequences, or non-safe characters.
    """
    if not isinstance(version_id, str) or not version_id:
        return False
    # Must not contain path separators or traversal
    if "/" in version_id or "\\" in version_id or ".." in version_id:
        return False
    return bool(_VERSION_ID_RE.match(version_id))


def _ensure_dirs(ledger_root: Path) -> None:
    """Create required subdirectories with restrictive permissions."""
    for subdir in ["recaps", "versions", "running"]:
        target = ledger_root / subdir
        target.mkdir(parents=True, exist_ok=True)
        try:
            target.chmod(0o700)
        except OSError:
            pass  # Best effort on restrictive permissions


def _version_timestamp() -> str:
    """Current UTC timestamp suitable for version directories.

    Uses microseconds and 12-digit random suffix to avoid collisions
    when multiple saves happen within the same second (e.g., tests).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    us = f"{datetime.now(timezone.utc).microsecond:06d}"
    # 12-digit random suffix for collision resistance in tight loops
    rnd = f"{random.getrandbits(80):012x}"[:12]
    return ts + f"_{us}_{rnd}"


def _atomic_write_json(
    target: Path,
    data: dict[str, Any],
) -> None:
    """Atomically write JSON with fsync + restrictive permissions.

    Existing file is only replaced after the temp file is fully flushed
    to disk. On failure the original remains intact.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(parent),
        suffix=".tmp",
        prefix=".recap_",
    )
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        encoded = content.encode("utf-8")

        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        fd = -1  # Mark as closed

        os.chmod(tmp_path, 0o600)

        os.rename(tmp_path, str(target))
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_text(
    target: Path,
    text: str,
) -> None:
    """Atomically write text with fsync + restrictive permissions."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(parent),
        suffix=".tmp",
        prefix=".recap_",
    )
    try:
        encoded = text.encode("utf-8")
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        fd = -1

        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, str(target))
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _install_current_pointer(current_ptr: Path, tmp_link: Path) -> None:
    """Install *tmp_link* as current without losing the previous current.

    Replacing an existing symlink or regular file is one atomic ``os.replace``.
    POSIX cannot replace a non-empty legacy directory with a symlink directly,
    so that one-time migration renames the directory aside and restores it if
    pointer installation fails.  The legacy backup is removed only after the
    new pointer is installed successfully.
    """
    if current_ptr.is_dir() and not current_ptr.is_symlink():
        legacy_backup = current_ptr.with_name(
            f".{current_ptr.name}.legacy-{os.getpid()}-{random.getrandbits(32):08x}"
        )
        os.rename(str(current_ptr), str(legacy_backup))
        try:
            os.replace(str(tmp_link), str(current_ptr))
        except BaseException:
            try:
                os.rename(str(legacy_backup), str(current_ptr))
            except OSError as restore_exc:
                logger.critical(
                    "Could not restore legacy current %s after pointer failure: %s",
                    current_ptr,
                    restore_exc,
                )
            raise
        else:
            try:
                shutil.rmtree(str(legacy_backup))
            except OSError as cleanup_exc:
                # The pointer swap already succeeded.  Keep the redundant old
                # directory for manual cleanup rather than reporting failure
                # after current has visibly changed.
                logger.warning(
                    "New current installed, but legacy cleanup failed at %s: %s",
                    legacy_backup,
                    cleanup_exc,
                )
        return

    # os.replace directly replaces an existing symlink, broken symlink, or
    # regular file.  Do not unlink first: that would destroy failure atomicity.
    os.replace(str(tmp_link), str(current_ptr))


def save_recap(
    date: str,
    data: dict[str, Any],
    source_fingerprint: str,
    generated_at: str | None = None,
    profile: str = "auxiliary.compression",
    model: str = "",
    ledger_root: Path | None = None,
) -> RecapVersion:
    """Save a recap atomically with immutable version archive.

    Design: ``recaps/<date>`` is an atomic symlink pointer to the immutable
    version dir under ``versions/<date>/<version_ts>/``.  Workflow:

    1. Create the complete immutable version directory first (meta.json, raw.json,
       summary.md).  Any write/snapshot failure aborts and preserves current.
    2. Atomically install a temporary relative symlink via os.replace so readers
       never see a dangling or partial pointer.
    3. On all failures the existing current (whether legacy dir or symlink) stays intact.

    Never mutates immutable versions after creation.

    Parameters
    ----------
    date :
        Calendar date in YYYY-MM-DD format.
    data :
        Validated recap JSON (session_summaries, overall_recap, cron_summary).
    source_fingerprint :
        SHA256 fingerprint of the source data this recap was generated from.
    generated_at :
        ISO-8601 UTC timestamp. Auto-generated if None.
    profile :
        Profile name used for generation.
    model :
        Model name used for generation.
    ledger_root :
        Override for tests.

    Returns
    -------
    RecapVersion with metadata about the saved version.

    Raises
    ------
    OSError
        If any write, snapshot, or symlink installation fails.
    """
    root = ledger_root or get_ledger_root()
    _ensure_dirs(root)

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    version_ts = _version_timestamp()
    session_count = len(data.get("session_summaries", []))
    cron_count = 1 if data.get("cron_summary") else 0

    # Build meta and markdown
    meta = {
        "date": date,
        "generated_at": generated_at,
        "collection_cutoff_utc": _next_chicago_midnight(date),
        "profile": profile,
        "model": model,
        "source_fingerprint": source_fingerprint,
        "version_id": version_ts,
    }

    markdown = _render_markdown(data, meta)

    # STEP 1: Create the complete immutable version directory FIRST.
    # Everything must succeed before we touch current.
    version_date_dir = root / "versions" / date
    version_date_dir.mkdir(parents=True, exist_ok=True)
    ver_dir = version_date_dir / version_ts
    # Immutable means collision must fail closed, never overwrite in place.
    ver_dir.mkdir(exist_ok=False)
    try:
        _atomic_write_json(ver_dir / "meta.json", meta)
        _atomic_write_json(ver_dir / "raw.json", data)
        _atomic_write_text(ver_dir / "summary.md", markdown)
    except BaseException:
        # Clean up incomplete version dir
        try:
            shutil.rmtree(str(ver_dir), ignore_errors=True)
        except OSError:
            pass
        raise

    # STEP 2: Atomically install symlink pointer.
    current_ptr = root / "recaps" / date
    current_ptr.parent.mkdir(parents=True, exist_ok=True)

    rel_target = os.path.relpath(str(ver_dir), str(current_ptr.parent))
    tmp_link = current_ptr.with_name(
        f".{current_ptr.name}.tmp-link-{version_ts}-{os.getpid()}-"
        f"{random.getrandbits(32):08x}"
    )
    try:
        os.symlink(rel_target, str(tmp_link))
        _install_current_pointer(current_ptr, tmp_link)
    except BaseException:
        # Rollback — remove temp symlink, immutable version stays for recovery
        try:
            tmp_link.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return RecapVersion(
        date=date,
        version_ts=version_ts,
        generated_at=generated_at,
        source_fingerprint=source_fingerprint,
        session_count=session_count,
        cron_count=cron_count,
    )


def _resolve_current(root: Path, date: str) -> Path | None:
    """Resolve the current recap directory for *date*.

    ``recaps/<date>`` may be a symlink pointing to ``versions/<date>/<ver_ts>``
    (new design) or a real directory (legacy pre-symlink design).  Returns the
    actual content directory or None if not found.
    """
    current_ptr = root / "recaps" / date

    # Symlink pointer (new design) — resolve to immutable version dir
    if current_ptr.is_symlink():
        target = current_ptr.resolve()
        if target.is_dir():
            return target
        return None  # Dangling symlink — treat as missing

    # Legacy real directory or non-existent
    if current_ptr.is_dir():
        return current_ptr

    return None


def load_recap(
    date: str,
    ledger_root: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load the current recap for a date.

    Returns ``(raw_data, meta)`` or ``(None, None)`` if not found.
    Never returns filesystem paths in the response.
    """
    root = ledger_root or get_ledger_root()
    recap_dir = _resolve_current(root, date)

    if recap_dir is None:
        return None, None

    meta_path = recap_dir / "meta.json"
    raw_path = recap_dir / "raw.json"

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        meta = {}

    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        raw = {}

    return raw, meta


def load_recap_markdown(
    date: str,
    ledger_root: Path | None = None,
) -> str | None:
    """Load the markdown summary for a date."""
    root = ledger_root or get_ledger_root()
    recap_dir = _resolve_current(root, date)

    if recap_dir is None:
        return None

    md_path = recap_dir / "summary.md"

    if not md_path.is_file():
        return None

    try:
        return md_path.read_text(encoding="utf-8")
    except OSError:
        return None


def list_versions(
    date: str,
    ledger_root: Path | None = None,
) -> list[RecapVersion]:
    """List all archived versions for a date (newest first)."""
    root = ledger_root or get_ledger_root()
    versions_dir = root / "versions" / date

    if not versions_dir.is_dir():
        return []

    versions: list[RecapVersion] = []
    for ver_dir in sorted(versions_dir.iterdir(), reverse=True):
        if not ver_dir.is_dir():
            continue

        meta_path = ver_dir / "meta.json"
        raw_path = ver_dir / "raw.json"

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            versions.append(RecapVersion(
                date=date,
                version_ts=meta.get("version_id", ver_dir.name),
                generated_at=meta.get("generated_at", ""),
                source_fingerprint=meta.get("source_fingerprint", ""),
                session_count=len(raw.get("session_summaries", [])),
                cron_count=1 if raw.get("cron_summary") else 0,
            ))
        except (json.JSONDecodeError, OSError):
            continue

    return versions


def rollback_to_version(
    date: str,
    version_ts: str,
    ledger_root: Path | None = None,
) -> RecapVersion | None:
    """Restore a specific archived version as the current recap.

    Atomically repoints ``recaps/<date>`` symlink to an already-complete
    immutable target under ``versions/<date>/<version_ts>/``.  Does NOT
    create inaccessible ``pre-rollback-*`` archive IDs — current already
    references an immutable version that stays untouched.

    Validates target completeness (meta.json + raw.json + summary.md)
    before switching.  On failure the existing current pointer is preserved.

    Returns the restored version's metadata or None if not found / incomplete.
    """
    root = ledger_root or get_ledger_root()
    _ensure_dirs(root)

    source = root / "versions" / date / version_ts
    if not source.is_dir():
        return None

    # Validate target completeness BEFORE any switch
    for fname in ("meta.json", "raw.json", "summary.md"):
        if not (source / fname).is_file():
            logger.warning(
                f"Incomplete rollback target {version_ts}: missing {fname}"
            )
            return None

    # Read target data to verify it's valid
    try:
        meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
        raw = json.loads((source / "raw.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Atomic repoint: create temp symlink to immutable target, swap via os.replace
    current_ptr = root / "recaps" / date
    current_ptr.parent.mkdir(parents=True, exist_ok=True)

    rel_target = os.path.relpath(str(source), str(current_ptr.parent))
    tmp_link = current_ptr.with_name(
        f".{current_ptr.name}.tmp-rollback-{version_ts}-{os.getpid()}-"
        f"{random.getrandbits(32):08x}"
    )

    try:
        os.symlink(rel_target, str(tmp_link))
        _install_current_pointer(current_ptr, tmp_link)
    except BaseException:
        # Rollback — remove temp symlink, existing current stays intact
        try:
            tmp_link.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return RecapVersion(
        date=date,
        version_ts=version_ts,
        generated_at=meta.get("generated_at", ""),
        source_fingerprint=meta.get("source_fingerprint", ""),
        session_count=len(raw.get("session_summaries", [])),
        cron_count=1 if raw.get("cron_summary") else 0,
    )


def recap_exists(date: str, ledger_root: Path | None = None) -> bool:
    """Check if a current recap exists for the given date."""
    root = ledger_root or get_ledger_root()
    return _resolve_current(root, date) is not None


def check_staleness(
    date: str,
    current_fingerprint: str,
    ledger_root: Path | None = None,
) -> bool:
    """Return True if the stored recap's fingerprint differs from current."""
    root = ledger_root or get_ledger_root()
    recap_dir = _resolve_current(root, date)

    if recap_dir is None:
        return False  # No recap means nothing is stale

    meta_path = recap_dir / "meta.json"

    if not meta_path.is_file():
        return False  # No recap means nothing is stale

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stored_fp = meta.get("source_fingerprint", "")
        return stored_fp != current_fingerprint
    except (json.JSONDecodeError, OSError):
        return True  # Corrupt metadata counts as stale


def _next_chicago_midnight(date_str: str) -> str:
    """Compute the next midnight UTC for a Chicago date."""
    from hermes_summarization_calendar.dates import chicago_next_midnight_utc

    dt = chicago_next_midnight_utc(date_str)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_markdown(data: dict[str, Any], meta: dict[str, Any]) -> str:
    """Render recap data as safe Markdown for storage and display."""
    from hermes_summarization_calendar.recap_validator import escape_markdown

    lines = [
        f"# Summarization Calendar Recap — {escape_markdown(meta.get('date', ''))}",
        "",
        f"**Generated:** {escape_markdown(meta.get('generated_at', ''))}  ",
        f"**Profile:** {escape_markdown(meta.get('profile', ''))}  ",
        f"**Model:** {escape_markdown(meta.get('model', ''))}  ",
        f"**Source fingerprint:** `{meta.get('source_fingerprint', '')}`",
        "",
        "## Overall Recap",
        "",
        escape_markdown(data.get("overall_recap", "")),
        "",
    ]

    if data.get("cron_summary"):
        lines.extend(["## Cron Summary", "", escape_markdown(data["cron_summary"]), ""])

    summaries = data.get("session_summaries", [])
    if summaries:
        lines.append("## Session Summaries")
        lines.append("")
        for s in sorted(summaries, key=lambda x: x.get("session_id", "")):
            sid = escape_markdown(s.get("session_id", ""))
            title = escape_markdown(s.get("title", ""))
            summary = escape_markdown(s.get("summary", ""))
            lines.extend([
                f"### Session `{sid}` — {title}",
                "",
                summary,
                "",
            ])

    return "\n".join(lines)
