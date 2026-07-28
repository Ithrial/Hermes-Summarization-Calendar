"""Shared pytest fixtures for Daily Ledger tests.

Creates in-memory SQLite databases with representative schemas matching
the live Hermes state.db and cron/executions.db layouts.
"""

from __future__ import annotations
import sys
import types


def _setup_mock_agent_auxiliary_client():
    """Create a mock agent.auxiliary_client module for tests.

    This is required because the tests patch agent.auxiliary_client.call_llm
    but the agent.auxiliary_client module may not be imported before the test runs.
    The mock module needs call_llm as an attribute so patch can find and replace it.
    """
    if 'agent' not in sys.modules:
        agent_module = types.ModuleType('agent')
        sys.modules['agent'] = agent_module

    if 'agent.auxiliary_client' not in sys.modules:
        auxiliary_client_module = types.ModuleType('agent.auxiliary_client')
        sys.modules['agent.auxiliary_client'] = auxiliary_client_module
        # Set a placeholder for call_llm so patch can find it
        auxiliary_client_module.call_llm = None


_setup_mock_agent_auxiliary_client()

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


def _create_state_db(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Create a test state.db with sessions and messages matching live schema."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # Schema matching live Hermes state.db
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT,
            cwd TEXT,
            rewind_count INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            git_branch TEXT,
            git_repo_root TEXT,
            session_key TEXT,
            chat_id TEXT,
            chat_type TEXT,
            thread_id TEXT,
            compression_failure_cooldown_until REAL,
            compression_failure_error TEXT,
            display_name TEXT,
            origin_json TEXT,
            expiry_finalized INTEGER DEFAULT 0,
            compression_fallback_streak INTEGER DEFAULT 0,
            profile_name TEXT,
            compression_ineffective_count INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()
    return conn, db_path


def _insert_test_data(conn: sqlite3.Connection) -> None:
    """Insert representative test sessions and messages.

    Creates sessions spanning multiple days including DST transition day.
    Chicago 2026-03-08 (spring forward: CDT starts).
    Chicago midnight UTC = 2026-03-08T06:00:00Z -> 2026-03-09T05:00:00Z (23h day)
    """
    # Timestamps for March 2026 Chicago day windows:
    # Mar 8 midnight CST = 2026-03-08T06:00:00Z (1772949600)
    # Mar 9 midnight CDT = 2026-03-09T05:00:00Z (1773032400) -> 23h spring-forward day

    # Session 1: March 7 activity (NOT in Mar 8 window)
    s1_id = "20260307_100000_aaa"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'cli', 'fixture-provider/fixture-model', 'Fix service configuration',
           ?, ?, 'default', 0, 0)""",
        (s1_id, 1772877600.0, 1772877700.0),  # Mar 7 10AM UTC
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s1_id, "fix the proxy", 1772877600.0),
    )

    # Session 2: Spring-forward DST day (March 8, 2026 - 23h)
    s2_id = "20260308_100000_bbb"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'telegram', 'fixture-provider/fixture-model', 'DST migration task',
           ?, ?, 'default', 0, 0)""",
        (s2_id, 1772960400.0, 1772978400.0),  # Mar 8 09:00-14:00 UTC -> within [06:00Z, next-day 05:00Z)
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s2_id, "check timezone handling", 1772964000.0),  # Mar 8 10AM UTC
    )

    # Session 3: Cross-midnight session (spans March 8 into March 9)
    s3_id = "20260308_220000_ccc"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'cli', 'fixture-provider/fixture-model', 'Late night debugging',
           ?, ?, 'default', 0, 0)""",
        (s3_id, 1772960400.0, 1773021600.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s3_id, "still debugging", 1773021600.0),  # Mar 9 02:00 UTC -> within Mar 8 window [06Z Mar8, 05Z Mar9)
    )

    # Session 4: Plugin-internal session (source='daily-ledger') — EXCLUDED
    s4_id = "20260308_recap_dd"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'daily-ledger', 'fixture-provider/fixture-model', 'Daily recap generation',
           ?, ?, 'default', 0, 0)""",
        (s4_id, 1772960400.0, 1772970000.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s4_id, "generate recap", 1772964000.0),
    )

    # Session 5: Inactive message (active=0) — should NOT count
    s5_id = "20260308_inactive_ee"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'cli', 'fixture-provider/fixture-model', 'Compacted session',
           ?, ?, 'default', 0, 0)""",
        (s5_id, 1772960400.0, 1772970000.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 0)",
        (s5_id, "compacted away", 1772964000.0),
    )

    # Session 6: Tool source session (should NOT be excluded)
    s6_id = "20260308_tool_ff"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'tool', 'fixture-provider/fixture-model', 'Tool call session',
           ?, ?, 'default', 0, 0)""",
        (s6_id, 1772960400.0, 1772970000.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s6_id, "tool call", 1772964000.0),
    )

    # Session 7: Fall-back DST day (November 1, 2026 - 25h)
    s7_id = "20261101_100000_ggg"
    conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'cli', 'fixture-provider/fixture-model', 'Fall back test',
           ?, ?, 'default', 0, 0)""",
        (s7_id, 1762598400.0, 1762609200.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        (s7_id, "25 hour day test", 1762602000.0),
    )

    conn.commit()


@pytest.fixture
def test_hermes_home(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Create a fake Hermes home with default + named profiles."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()

    # Default profile state.db
    (hermes_home / "profiles").mkdir()
    default_db_dir = hermes_home
    default_conn, default_db = _create_state_db(tmp_path)
    _insert_test_data(default_conn)
    default_conn.close()
    default_db.rename(hermes_home / "state.db")

    # Named profile: named-profile
    named_dir = hermes_home / "profiles" / "named-profile"
    named_dir.mkdir(parents=True)
    named_conn, named_db = _create_state_db(tmp_path)
    # Add one session to named-profile (within Mar 8 Chicago window)
    named_conn.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at,
           profile_name, message_count, tool_call_count)
           VALUES (?, 'cli', 'custom:named-profile', 'Named profile test',
           ?, ?, 'named-profile', 0, 0)""",
        ("named_20260308_001", 1772964000.0, 1772970000.0),
    )
    named_conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
        ("named_20260308_001", "hello named-profile", 1772964000.0),
    )
    named_conn.commit()
    named_conn.close()
    named_db.rename(named_dir / "state.db")

    # Cron root
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir()

    # executions.db
    exec_conn = sqlite3.connect(str(cron_dir / "executions.db"))
    exec_conn.execute("""
        CREATE TABLE executions (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            source TEXT NOT NULL,
            process_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            process_started_at INTEGER,
            status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
            claimed_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT
        )
    """)
    # Execution within March 8 day (Chicago midnight: 2026-03-08T06:00:00Z)
    exec_conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at, error)
           VALUES (?, 'job1', 'builtin', 'proc1', 12345,
           'completed', '2026-03-08T09:00:00.000000-06:00',
           '2026-03-08T09:00:01.000000-06:00',
           '2026-03-08T09:05:00.000000-06:00', NULL)""",
        ("exec_001",),
    )
    # Failed execution with error containing PID/path
    exec_conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at, error)
           VALUES (?, 'job2', 'builtin', 'proc2', 99999,
           'failed', '2026-03-08T10:00:00.000000-06:00',
           '2026-03-08T10:00:01.000000-06:00', NULL,
           'pid=99999 failed with exit code 1 at /home/alice/some/path')""",
        ("exec_002",),
    )
    exec_conn.commit()
    exec_conn.close()

    # jobs.json
    jobs_data = {
        "jobs": [
            {"id": "job1", "name": "Daily briefing", "prompt": "..."},
            {"id": "job2", "name": "System health check", "prompt": "..."},
        ],
        "updated_at": "2026-03-08T00:00:00Z",
    }
    (cron_dir / "jobs.json").write_text(json.dumps(jobs_data))

    return hermes_home, jobs_data


@pytest.fixture
def empty_hermes_home(tmp_path: Path) -> Path:
    """Create an empty Hermes home directory structure."""
    home = tmp_path / ".hermes_empty"
    home.mkdir()
    (home / "state.db").touch()
    # Empty state.db with schema
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL,
        model TEXT, title TEXT, started_at REAL NOT NULL, ended_at REAL,
        profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
        tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        tool_calls TEXT)""")
    conn.commit()
    conn.close()
    return home
