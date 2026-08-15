"""Read-only Hermes session and cron inventory.

Discovers profile state databases, queries them in strict read-only mode,
and produces per-day session/cron metadata.  Never modifies source files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_summarization_calendar.contract import (
    DailyCronRun,
    DailySession,
    DayCell,
    DayInventory,
    FingerprintComponent,
    MonthInventory,
    SourceReadability,
    _safe_job_name,
    compute_source_fingerprint,
)
from hermes_summarization_calendar.dates import (
    chicago_day_window_utc,
    days_in_month,
    date_str_from_ymd,
    unix_ts_to_utc_iso,
)

# Sessions with one of these ``source`` values belong to the plugin itself
# and must be excluded so recap generation does not include its own output.
# "summarization-calendar" is the current tag; "daily-ledger" is the legacy
# tag written by v1.1.0-and-earlier installs (Hermes core tags the
# auxiliary-compression sub-session the plugin uses). Both must be excluded
# so a renamed install never indexes its own pre-rename recap sessions.
_PLUGIN_SOURCE = "summarization-calendar"
_LEGACY_PLUGIN_SOURCE = "daily-ledger"
_PLUGIN_SOURCES = (_PLUGIN_SOURCE, _LEGACY_PLUGIN_SOURCE)


# ---------------------------------------------------------------------------
# Profile discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileSource:
    """A single canonical state.db discovered on disk."""

    db_path: Path
    label: str  # e.g. "default", "named-profile", "auxiliary.compression"


@dataclass(frozen=True)
class CronRoot:
    """A single canonical cron root directory with its profile label."""

    cron_dir: Path
    label: str


def get_hermes_home() -> Path:
    """Return HERMES_HOME or the default ``~/.hermes``."""
    val = os.environ.get("HERMES_HOME")
    if val:
        return Path(val).expanduser()
    return Path.home() / ".hermes"


def discover_profiles(hermes_home: Path | None = None) -> list[ProfileSource]:
    """Find all canonical state.db files.

    Returns the default profile (``<HERMES_HOME>/state.db``) first, followed
    by each immediate sub-directory under ``profiles/<name>/state.db``.
    Symlink escapes, backups, and snapshots are ignored.
    """
    if hermes_home is None:
        hermes_home = get_hermes_home()
    home = hermes_home.resolve()

    results: list[ProfileSource] = []

    # Default profile
    default_db = home / "state.db"
    if _is_canonical_db(default_db, home):
        results.append(ProfileSource(db_path=default_db, label="default"))

    # Named profiles
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            if not child.is_dir():
                continue
            profile_db = child / "state.db"
            if _is_canonical_db(profile_db, home):
                results.append(
                    ProfileSource(db_path=profile_db, label=child.name)
                )

    return results


def discover_cron_roots(hermes_home: Path | None = None) -> list[CronRoot]:
    """Find all canonical cron roots.

    Returns ``<HERMES_HOME>/cron`` (label "default") plus each immediate
    ``<HERMES_HOME>/profiles/<name>/cron``, rejecting symlink escapes.
    A root with only jobs.json and no executions.db is harmless — it
    contributes zero runs.
    """
    if hermes_home is None:
        hermes_home = get_hermes_home()
    home = hermes_home.resolve()

    roots: list[CronRoot] = []

    # Default cron root
    default_cron = home / "cron"
    if _is_canonical_dir(default_cron, home):
        roots.append(CronRoot(cron_dir=default_cron, label="default"))

    # Profile-specific cron roots
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            if not child.is_dir():
                continue
            profile_cron = child / "cron"
            if _is_canonical_dir(profile_cron, home):
                roots.append(CronRoot(cron_dir=profile_cron, label=child.name))

    return roots


def _is_canonical_db(path: Path, hermes_home: Path) -> bool:
    """Return True if *path* is a real file that resolves under *hermes_home*.

    Rejects symlinks pointing outside the home directory, backup dirs,
    and snapshot directories.
    """
    if not path.is_file():
        return False
    try:
        resolved = path.resolve()
        resolved.relative_to(hermes_home)
    except ValueError:
        return False
    parts = path.parts
    for p in parts:
        if p.startswith("state-snapshots") or p.endswith(".bak"):
            return False
    return True


def _is_canonical_dir(path: Path, hermes_home: Path) -> bool:
    """Return True if *path* is a real directory that resolves under *hermes_home*.

    Rejects symlinks pointing outside the home directory.
    """
    if not path.is_dir():
        return False
    try:
        resolved = path.resolve()
        resolved.relative_to(hermes_home)
    except ValueError:
        return False
    return True


def get_cron_root(hermes_home: Path | None = None) -> Path | None:
    """Legacy single-cron-root helper. Returns the first found cron root."""
    roots = discover_cron_roots(hermes_home)
    return roots[0].cron_dir if roots else None


# ---------------------------------------------------------------------------
# Read-only SQLite helpers
# ---------------------------------------------------------------------------


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open *path* as a strict read-only connection.

    Uses URI ``mode=ro``, sets ``query_only=ON``, and adds a busy timeout.
    Attempting any write raises ``sqlite3.DatabaseError``.
    """
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Session queries
# ---------------------------------------------------------------------------


def _is_source_excluded(source: str | None) -> bool:
    """Return True for plugin-internal sessions that must be excluded."""
    return source in _PLUGIN_SOURCES


def _safe_title(raw_title: str | None, session_id: str) -> str:
    """Return a non-empty title.  Whitespace-only titles fall back to 'Session <id>'.

    Profile_name from the DB row is NOT used for identity — that comes from
    the canonical path label.  profile_name here is only metadata/cross-check.
    """
    if raw_title and raw_title.strip():
        return raw_title.strip()
    return f"Session {session_id}"


def _count_tool_calls_json(tool_calls_json: str | None) -> int:
    """Count tool calls from a JSON tool_calls array, conservatively.

    Returns the length of the parsed array, or 0 if not valid JSON.
    Never crashes on malformed data.
    """
    if not tool_calls_json or not isinstance(tool_calls_json, str):
        return 0
    try:
        parsed = json.loads(tool_calls_json)
        if isinstance(parsed, list):
            return len(parsed)
        return 0
    except (json.JSONDecodeError, TypeError):
        return 0


def query_day_sessions(
    db_path: Path,
    profile_label: str,
    start_utc: float,
    end_utc: float,
) -> tuple[list[DailySession], list[FingerprintComponent]]:
    """Find sessions with active messages in ``[start_utc, end_utc)``.

    Returns (sessions, fingerprint_components).  Both lists are sorted by
    session_id for determinism.

    *profile_label* is the canonical path-derived label used for all API
    responses.  The DB row's ``profile_name`` field is treated as an
    optional cross-check and never exposed in API output.
    """
    conn = open_readonly(db_path)
    try:
        # Find sessions that have active messages within the window,
        # excluding plugin-internal sessions.
        query = """
            SELECT DISTINCT s.id, s.source, s.model, s.title
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE m.active = 1
              AND m.timestamp >= ?
              AND m.timestamp < ?
              AND (s.source NOT IN (?, ?) OR s.source IS NULL)
            ORDER BY s.id
        """
        rows = conn.execute(
            query, (start_utc, end_utc, *_PLUGIN_SOURCES)
        ).fetchall()

        sessions: list[DailySession] = []
        components: list[FingerprintComponent] = []

        for row in rows:
            sid = row["id"]
            source = row["source"] or ""
            model = row["model"] or ""
            raw_title = row["title"]

            # SAFE TITLE — use canonical profile_label, never stale profile_name
            title = _safe_title(raw_title, sid)

            # Count messages and tool calls within the window for this session
            msg_query = """
                SELECT COUNT(*) AS mc,
                       SUM(CASE WHEN tool_name IS NOT NULL OR tool_calls IS NOT NULL THEN 1 ELSE 0 END) AS tc_rows
                FROM messages
                WHERE session_id = ? AND active = 1
                  AND timestamp >= ? AND timestamp < ?
            """
            msg_row = conn.execute(msg_query, (sid, start_utc, end_utc)).fetchone()
            msg_count = msg_row["mc"] or 0

            # Tool call counting: single pass with deferred tool-result dedup.
            # 1) Count JSON tool_calls array entries (assistant rows).
            #    Track which tool_names appear in those arrays.
            # 2) For role='tool' result rows, count only if no assistant JSON
            #    already referenced that tool_name.
            # 3) Rows with tool_name but no JSON and non-tool role count as 1.
            tc_query = """
                SELECT role, tool_calls, tool_name
                FROM messages
                WHERE session_id = ? AND active = 1
                  AND timestamp >= ? AND timestamp < ?
                ORDER BY id
            """
            tc_rows = conn.execute(tc_query, (sid, start_utc, end_utc)).fetchall()

            # First pass: find tool names covered by assistant JSON arrays
            json_covered_tools: set[str] = set()
            for tr in tc_rows:
                if tr["tool_calls"]:
                    try:
                        parsed = json.loads(tr["tool_calls"])
                        if isinstance(parsed, list):
                            for call in parsed:
                                if isinstance(call, dict) and call.get("name"):
                                    json_covered_tools.add(call["name"])
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Second pass: count tool calls without double-counting
            tc_count = 0
            for tr in tc_rows:
                role = tr["role"] or ""
                json_calls = _count_tool_calls_json(tr["tool_calls"])
                has_tool_name = bool(tr["tool_name"])

                if json_calls > 0:
                    # Assistant row with tool_calls JSON array — count entries
                    tc_count += json_calls
                elif has_tool_name and role == "tool":
                    # Tool result row — only count if not covered by assistant JSON
                    tn = tr["tool_name"]
                    if tn not in json_covered_tools:
                        tc_count += 1
                elif has_tool_name:
                    # Non-tool row with tool_name (e.g., assistant that didn't use JSON)
                    tc_count += 1

            # First/last active timestamps within window
            ts_query = """
                SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
                FROM messages
                WHERE session_id = ? AND active = 1
                  AND timestamp >= ? AND timestamp < ?
            """
            ts_row = conn.execute(ts_query, (sid, start_utc, end_utc)).fetchone()

            # Fingerprint components: hash full message identity fields.
            # Never return raw content — digest only.
            fp_query = """
                SELECT id, session_id, role, timestamp, active, compacted,
                       content, tool_calls, tool_name
                FROM messages
                WHERE session_id = ? AND active = 1
                  AND timestamp >= ? AND timestamp < ?
                ORDER BY id
            """
            fp_rows = conn.execute(fp_query, (sid, start_utc, end_utc)).fetchall()
            session_components: list[FingerprintComponent] = []
            for fr in fp_rows:
                # Full SHA-256 of all relevant fields — never truncated
                hash_input = "|".join([
                    str(fr["id"] or ""),
                    str(fr["session_id"] or ""),
                    str(fr["role"] or ""),
                    str(fr["timestamp"] or ""),
                    str(fr["active"] or ""),
                    str(fr["compacted"] or ""),
                    str(fr["content"] or ""),
                    str(fr["tool_calls"] or ""),
                    str(fr["tool_name"] or ""),
                ])
                digest = hashlib.sha256(hash_input.encode("utf-8", errors="replace")).hexdigest()

                session_components.append(
                    FingerprintComponent(
                        session_id=fr["session_id"],
                        profile_label=profile_label,  # Canonical label for cross-DB determinism
                        timestamp=fr["timestamp"],
                        role=fr["role"] or "",
                        active=fr["active"],
                        content_digest=digest,
                    )
                )

            components.extend(session_components)
            sessions.append(
                DailySession(
                    session_id=sid,
                    profile=profile_label,  # Canonical label, NOT stale profile_name
                    source=source,
                    model=model,
                    title=title,
                    message_count=msg_count,
                    tool_call_count=tc_count,
                    first_active_utc=unix_ts_to_utc_iso(ts_row["first_ts"] or None),
                    last_active_utc=unix_ts_to_utc_iso(ts_row["last_ts"] or None),
                    source_fingerprint=compute_source_fingerprint(session_components),
                )
            )

        return sessions, components
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cron queries
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(ts_str: str | None) -> float | None:
    """Parse an ISO-8601 timestamp string with offset to UTC epoch seconds.

    Handles ``Z`` suffix, ``+HH:MM`` / ``-HH:MM`` offsets, and naive strings
    (treated as UTC).  Returns None for empty/invalid values instead of crashing.
    """
    if not ts_str or not isinstance(ts_str, str):
        return None
    ts_str = ts_str.strip()
    if not ts_str:
        return None

    try:
        # Python 3.11 datetime.fromisoformat handles most cases
        dt = datetime.fromisoformat(ts_str)
        # If naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _sanitize_error(raw: str) -> str | None:
    """Remove PIDs, absolute paths, and secrets from error text.

    Returns a bounded string (max 200 chars) safe for API responses.
    """
    if not raw:
        return None
    sanitized = raw.strip()[:200]
    # Remove PIDs
    sanitized = re.sub(r"\bpid[_\s]?=?\s*\d+", "[PID]", sanitized, flags=re.I)
    # Remove absolute file paths (all system dirs)
    sanitized = re.sub(
        r"/(?:home|var|tmp|opt|mnt|root|usr|etc)[\w./-]+", "[PATH]", sanitized
    )
    # Remove potential secrets (long hex strings that look like tokens)
    sanitized = re.sub(r"\b[0-9a-f]{32,}\b", "[REDACTED]", sanitized)
    return sanitized if sanitized else None


def _error_digest(raw: str | None) -> str:
    """Compute a bounded digest of sanitized error for fingerprinting."""
    sanitized = _sanitize_error(raw or "")
    return hashlib.sha256((sanitized or "").encode("utf-8", errors="replace")).hexdigest()[:32]


def query_cron_runs_for_root(
    cron_dir: Path,
    label: str,
    start_utc: float,
    end_utc: float,
) -> tuple[list[DailyCronRun], list[FingerprintComponent]]:
    """Find cron executions from a single cron root.

    Parses ISO timestamps with offsets (e.g., ``-05:00``, ``Z``) and compares
    as UTC epoch instants.  Invalid/empty timestamps are skipped silently.

    Returns (runs, fingerprint_components).
    """
    exec_db = cron_dir / "executions.db"
    jobs_json = cron_dir / "jobs.json"

    if not exec_db.is_file():
        return [], []

    # Load job names from jobs.json
    job_names: dict[str, str] = {}
    if jobs_json.is_file():
        try:
            raw = json.loads(jobs_json.read_text(encoding="utf-8"))
            jobs_list = raw.get("jobs", []) if isinstance(raw, dict) else raw
            if isinstance(jobs_list, list):
                for job in jobs_list:
                    if isinstance(job, dict):
                        jid = job.get("id")
                        jname = job.get("name", jid or "")
                        if jid:
                            job_names[jid] = _safe_job_name(str(jid), jname)
        except Exception:
            pass

    conn = open_readonly(exec_db)
    try:
        # Fetch all executions — filter by timestamp in Python to handle offsets
        rows = conn.execute(
            "SELECT id, job_id, source, status, claimed_at, started_at, finished_at, error "
            "FROM executions ORDER BY claimed_at",
        ).fetchall()

        runs: list[DailyCronRun] = []
        components: list[FingerprintComponent] = []

        for row in rows:
            claimed_ts = _parse_iso_timestamp(row["claimed_at"])
            if claimed_ts is None:
                # Invalid/empty claimed_at — skip silently
                continue

            # Timezone-aware comparison as UTC epoch instants
            if not (start_utc <= claimed_ts < end_utc):
                continue

            started_ts_str = row["started_at"]
            finished_ts_str = row["finished_at"]

            runs.append(
                DailyCronRun(
                    execution_id=row["id"],
                    job_id=row["job_id"],
                    job_name=job_names.get(row["job_id"], _safe_job_name(row["job_id"], None)),
                    profile=label,  # Canonical cron root label
                    source=row["source"] or "unknown",
                    status=row["status"] or "unknown",
                    claimed_at=row["claimed_at"],
                    started_at=started_ts_str,
                    finished_at=finished_ts_str,
                    error_summary=_sanitize_error(row["error"]),
                )
            )

            # Fingerprint component for cron execution
            eid = row["id"]
            jid = row["job_id"]
            claimed_parsed = _parse_iso_timestamp(row["claimed_at"]) or 0.0
            status_val = (row["status"] or "unknown").strip()
            source_val = (row["source"] or "").strip()

            # Digest for stable cron fingerprint (no raw content/paths)
            cron_hash_parts = "|".join([
                eid, jid, label, source_val, status_val,
                str(claimed_parsed),
                _error_digest(row["error"]),
            ])
            cron_digest = hashlib.sha256(cron_hash_parts.encode("utf-8")).hexdigest()

            components.append(
                FingerprintComponent(
                    session_id=f"cron:{eid}",  # Unique prefix for cron entries
                    profile_label=label,
                    timestamp=claimed_parsed,
                    role="cron",
                    active=1,
                    content_digest=cron_digest,
                )
            )

        return runs, components
    finally:
        conn.close()


def query_day_cron_runs(
    cron_roots: list[CronRoot] | None,
    start_utc: float,
    end_utc: float,
) -> tuple[list[DailyCronRun], list[FingerprintComponent]]:
    """Find cron executions across all discovered cron roots.

    Aggregates runs from the default root plus each profile-specific root.
    Each execution is tagged with its canonical root label.

    Returns (all_runs, fingerprint_components).
    """
    if not cron_roots:
        return [], []

    all_runs: list[DailyCronRun] = []
    all_components: list[FingerprintComponent] = []

    for cr in cron_roots:
        try:
            runs, components = query_cron_runs_for_root(
                cr.cron_dir, cr.label, start_utc, end_utc
            )
            all_runs.extend(runs)
            all_components.extend(components)
        except Exception:
            # Skip roots that can't be read — don't crash the endpoint
            pass

    return all_runs, all_components


# ---------------------------------------------------------------------------
# Month-level aggregation
# ---------------------------------------------------------------------------


def build_month_inventory(
    year: int,
    month: int,
    profiles: list[ProfileSource],
    cron_roots: list[CronRoot] | None,
) -> MonthInventory:
    """Build a month grid by scanning each day's sessions and cron runs."""
    num_days = days_in_month(year, month)
    cells: list[DayCell] = []

    for day in range(1, num_days + 1):
        date_str = date_str_from_ymd(year, month, day)
        start_utc_dt, end_utc_dt = chicago_day_window_utc(date_str)
        start_ts = start_utc_dt.timestamp()
        end_ts = end_utc_dt.timestamp()

        # Collect sessions across all profiles
        total_sessions = 0
        for ps in profiles:
            try:
                sessions, _ = query_day_sessions(ps.db_path, ps.label, start_ts, end_ts)
                total_sessions += len(sessions)
            except Exception:
                pass

        # Collect cron runs across all roots
        cron_runs, _ = [], []
        try:
            cron_runs, _ = query_day_cron_runs(cron_roots, start_ts, end_ts)
        except Exception:
            pass

        cells.append(
            DayCell(
                date=date_str,
                active=total_sessions > 0 or len(cron_runs) > 0,
                session_count=total_sessions,
                cron_run_count=len(cron_runs),
                has_recap=False,
                recap_stale=False,
            )
        )

    return MonthInventory(year=year, month=month, days=cells)


def build_day_inventory(
    date_str: str,
    profiles: list[ProfileSource],
    cron_roots: list[CronRoot] | None,
) -> DayInventory:
    """Build full day inventory for a single calendar date."""
    start_utc_dt, end_utc_dt = chicago_day_window_utc(date_str)

    all_sessions: list[DailySession] = []
    all_components: list[FingerprintComponent] = []

    for ps in profiles:
        try:
            sessions, components = query_day_sessions(
                ps.db_path, ps.label, start_utc_dt.timestamp(), end_utc_dt.timestamp()
            )
            all_sessions.extend(sessions)
            all_components.extend(components)
        except Exception:
            pass

    # Sort sessions by session_id for determinism
    all_sessions.sort(key=lambda s: s.session_id)

    cron_runs: list[DailyCronRun] = []
    cron_components: list[FingerprintComponent] = []
    try:
        cron_runs, cron_components = query_day_cron_runs(
            cron_roots, start_utc_dt.timestamp(), end_utc_dt.timestamp()
        )
    except Exception:
        pass

    # Fingerprint includes BOTH session and cron components
    all_fp = all_components + cron_components
    fingerprint = compute_source_fingerprint(all_fp)

    return DayInventory(
        date=date_str,
        chicago_midnight_utc=start_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        chicago_next_midnight_utc=end_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        sessions=all_sessions,
        cron_runs=cron_runs,
        source_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _sanitize_health_error(exc: Exception) -> str:
    """Return a bounded generic error class without filesystem paths."""
    err_type = type(exc).__name__
    # Strip any path info from the exception message
    msg = str(exc).strip()[:120]
    # Remove absolute paths
    msg = re.sub(r"/(?:home|var|tmp|opt|mnt)[\w./-]+", "[PATH]", msg)
    return f"{err_type}: {msg}" if msg else err_type


def check_health(
    profiles: list[ProfileSource],
    cron_roots: list[CronRoot] | None,
) -> tuple[list[SourceReadability], bool]:
    """Check readability of all discovered sources.

    Returns (source_readabilities, cron_readable).

    Error messages never contain absolute filesystem paths.
    """
    sources: list[SourceReadability] = []
    for ps in profiles:
        try:
            conn = open_readonly(ps.db_path)
            count = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
            conn.close()
            sources.append(
                SourceReadability(
                    profile_label=ps.label,
                    readable=True,
                    session_count=count,
                    error=None,
                )
            )
        except Exception as exc:
            sanitized_error = _sanitize_health_error(exc)
            sources.append(
                SourceReadability(
                    profile_label=ps.label,
                    readable=False,
                    session_count=None,
                    error=sanitized_error,
                )
            )

    cron_readable = False
    if cron_roots:
        for cr in cron_roots:
            exec_db = cr.cron_dir / "executions.db"
            if exec_db.is_file():
                try:
                    conn = open_readonly(exec_db)
                    conn.execute("SELECT COUNT(*) FROM executions")
                    conn.close()
                    cron_readable = True
                    break
                except Exception:
                    pass

    return sources, cron_readable


# ---------------------------------------------------------------------------
# Public convenience — discover + verify all at once
# ---------------------------------------------------------------------------


def discover_all(hermes_home: Path | None = None) -> tuple[list[ProfileSource], list[CronRoot]]:
    """Discover profiles and cron roots in one call."""
    home = hermes_home or get_hermes_home()
    profiles = discover_profiles(home)
    cron_roots = discover_cron_roots(home)
    return profiles, cron_roots
