"""Frozen API contract for Hermes Summarization Calendar.

This module defines every JSON type that frontend/recap workers consume
from the plugin API.  After the initial commit no consumer should need
to edit this file — add new optional fields only when the producer is
ready to provide them.

All timestamps are ISO-8601 UTC strings (trailing ``Z``) unless noted
otherwise.  No raw message content, system prompts, or absolute source
database paths ever appear in any response type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Day-level types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailySession:
    """Per-session metadata for a single calendar day."""

    session_id: str
    profile: str
    source: str
    model: str
    title: str
    message_count: int
    tool_call_count: int
    first_active_utc: str | None = None
    last_active_utc: str | None = None
    source_fingerprint: str = ""


@dataclass(frozen=True)
class DailyCronRun:
    """Per-cron-execution metadata for a single calendar day."""

    execution_id: str
    job_id: str
    job_name: str
    profile: str
    source: str
    status: str
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_summary: str | None = None


def _safe_job_name(jid: str, fallback: str | None) -> str:
    """Return a safe non-empty job name."""
    if fallback and isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return jid or "unknown"


@dataclass(frozen=True)
class DayInventory:
    """Full inventory response for one calendar day (GET /day)."""

    date: str
    chicago_midnight_utc: str
    chicago_next_midnight_utc: str
    sessions: list[DailySession] = field(default_factory=list)
    cron_runs: list[DailyCronRun] = field(default_factory=list)
    source_fingerprint: str = ""


@dataclass(frozen=True)
class DayCell:
    """Single day cell inside a month grid (GET /month)."""

    date: str
    active: bool
    session_count: int
    cron_run_count: int
    has_recap: bool
    recap_stale: bool = False


@dataclass(frozen=True)
class MonthInventory:
    """Month grid response (GET /month)."""

    year: int
    month: int
    days: list[DayCell] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Health types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceReadability:
    """Read-only accessibility of a single Hermes source."""

    profile_label: str
    readable: bool
    session_count: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class HealthStatus:
    """Health check response (GET /health)."""

    status: str
    plugin_name: str
    version: str
    profiles_discovered: int
    readable_sources: int
    cron_readable: bool
    unreadable_sources: list[str] = field(default_factory=list)
    sources: list[SourceReadability] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Source fingerprint helpers (no raw content)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FingerprintComponent:
    """One stable piece of identity for the source fingerprint.

    Contains only metadata, never message body text.  Sorted and hashed
    deterministically so that adding/removing/changing meaningful activity
    always changes the fingerprint while cosmetic reordering does not.
    """

    session_id: str
    profile_label: str
    timestamp: float
    role: str
    active: int
    content_digest: str  # sha256 hex of *truncated* content for stability


def compute_source_fingerprint(components: list[FingerprintComponent]) -> str:
    """Deterministic SHA-256 hex over sorted fingerprint components.

    Excludes volatile generated fields (``finished_at``, process IDs, etc.).
    Stable across query order — always sorted by session_id then timestamp.
    """
    import hashlib
    import json

    sorted_items = sorted(
        components,
        key=lambda c: (c.profile_label, c.session_id, c.timestamp),
    )
    raw_parts = []
    for c in sorted_items:
        raw_parts.append(
            f"{c.session_id}|{c.profile_label}|{c.timestamp}"
            f"|{c.role}|{c.active}|{c.content_digest}"
        )
    blob = "\n".join(raw_parts)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers to convert dataclasses to JSON-safe dicts (for FastAPI responses)
# ---------------------------------------------------------------------------


def _dataclass_to_dict(obj: object) -> dict:
    """Convert a frozen dataclass to a flat dict for API serialization."""
    import dataclasses as dc
    if not dc.is_dataclass(obj):
        raise TypeError(f"Expected dataclass, got {type(obj).__name__}")
    return {f.name: getattr(obj, f.name) for f in dc.fields(obj)}


def session_to_dict(s: DailySession) -> dict:
    return _dataclass_to_dict(s)


def cron_run_to_dict(c: DailyCronRun) -> dict:
    return _dataclass_to_dict(c)


def day_cell_to_dict(d: DayCell) -> dict:
    return _dataclass_to_dict(d)


def month_to_dict(m: MonthInventory) -> dict:
    return {
        "year": m.year,
        "month": m.month,
        "days": [day_cell_to_dict(d) for d in m.days],
    }


def day_inventory_to_dict(d: DayInventory) -> dict:
    return {
        "date": d.date,
        "chicago_midnight_utc": d.chicago_midnight_utc,
        "chicago_next_midnight_utc": d.chicago_next_midnight_utc,
        "sessions": [session_to_dict(s) for s in d.sessions],
        "cron_runs": [cron_run_to_dict(c) for c in d.cron_runs],
        "source_fingerprint": d.source_fingerprint,
    }


def health_to_dict(h: HealthStatus) -> dict:
    return {
        "status": h.status,
        "plugin_name": h.plugin_name,
        "version": h.version,
        "profiles_discovered": h.profiles_discovered,
        "readable_sources": h.readable_sources,
        "unreadable_sources": h.unreadable_sources,
        "cron_readable": h.cron_readable,
        "sources": [_dataclass_to_dict(s) for s in h.sources],
    }
