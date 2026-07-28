"""Server-side transcript collection for recap generation.

Reads active user/assistant/tool messages from canonical profile DB paths
using URI mode=ro + query_only. Excludes system prompts, reasoning fields,
and sessions with source='daily-ledger'. Never returns raw transcripts in
HTTP responses — data stays server-side and is only used to build the
prompt payload for the LLM compression task.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_daily_ledger.inventory import ProfileSource

# Only these roles carry content useful for a faithful recap
_ALLOWED_ROLES = {"user", "assistant", "tool"}

# Plugin-internal sessions that must never be recapped
_PLUGIN_SOURCE = "daily-ledger"


@dataclass(frozen=True)
class TranscriptMessage:
    """One message extracted for recap input. No system/reasoning data."""

    role: str
    content: str | None
    tool_name: str | None = None


@dataclass(frozen=True)
class SessionTranscript:
    """All relevant messages from one session on a given day."""

    session_id: str
    profile: str
    title: str
    source: str
    model: str
    messages: list[TranscriptMessage]


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open *path* as strict read-only connection (reuses inventory pattern)."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def collect_session_transcript(
    db_path: Path,
    session_id: str,
    profile_label: str,
    start_utc: float,
    end_utc: float,
) -> SessionTranscript | None:
    """Collect active messages for *session_id* within the day window.

    Returns ``None`` if the session does not exist or has no eligible messages.
    Excludes role='system', reasoning columns, and source='daily-ledger'.
    """
    conn = _open_readonly(db_path)
    try:
        # Verify session exists and is not plugin-internal
        row = conn.execute(
            "SELECT id, source, model, title FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None

        source = row["source"] or ""
        if source == _PLUGIN_SOURCE:
            return None

        title = (row["title"] or "").strip()
        if not title:
            title = f"Session {session_id}"

        # Fetch active user/assistant/tool messages in chronological order.
        # Deliberately excludes reasoning, reasoning_content, reasoning_details,
        # codex_reasoning_items, codex_message_items columns.
        query = """
            SELECT role, content, tool_name
            FROM messages
            WHERE session_id = ?
              AND active = 1
              AND timestamp >= ?
              AND timestamp < ?
              AND role IN ('user', 'assistant', 'tool')
            ORDER BY id
        """
        rows = conn.execute(query, (session_id, start_utc, end_utc)).fetchall()

        messages: list[TranscriptMessage] = []
        for r in rows:
            messages.append(
                TranscriptMessage(
                    role=r["role"] or "unknown",
                    content=r["content"],
                    tool_name=r["tool_name"],
                )
            )

        if not messages:
            return None

        return SessionTranscript(
            session_id=session_id,
            profile=profile_label,
            title=title,
            source=source,
            model=row["model"] or "",
            messages=messages,
        )
    finally:
        conn.close()


def collect_day_transcripts(
    db_path: Path,
    profile_label: str,
    start_utc: float,
    end_utc: float,
) -> list[SessionTranscript]:
    """Collect transcripts for ALL eligible sessions in a day window.

    Returns an empty list if no sessions match — never ``None``.
    Sorted by session_id for determinism.
    """
    conn = _open_readonly(db_path)
    try:
        query = """
            SELECT DISTINCT s.id, s.source, s.model, s.title
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE m.active = 1
              AND m.timestamp >= ?
              AND m.timestamp < ?
              AND (s.source != ? OR s.source IS NULL)
            ORDER BY s.id
        """
        rows = conn.execute(
            query, (start_utc, end_utc, _PLUGIN_SOURCE)
        ).fetchall()

        transcripts: list[SessionTranscript] = []
        for row in rows:
            st = collect_session_transcript(
                db_path,
                row["id"],
                profile_label,
                start_utc,
                end_utc,
            )
            if st is not None:
                transcripts.append(st)

        return sorted(transcripts, key=lambda t: t.session_id)
    finally:
        conn.close()


def collect_all_day_transcripts(
    profiles: list["ProfileSource"],
    start_utc: float,
    end_utc: float,
) -> list[SessionTranscript]:
    """Collect transcripts across all discovered profile DBs.

    Returns a flat list sorted by (profile, session_id) for determinism.
    Import ProfileSource locally to avoid circular deps when tests mock it.
    """
    from hermes_daily_ledger.inventory import ProfileSource  # noqa: PLC2801

    all_transcripts: list[SessionTranscript] = []

    for ps in profiles:
        try:
            transcripts = collect_day_transcripts(
                ps.db_path, ps.label, start_utc, end_utc
            )
            all_transcripts.extend(transcripts)
        except Exception:
            # Skip unreadable profiles — don't crash the whole day
            pass

    return sorted(all_transcripts, key=lambda t: (t.profile, t.session_id))
