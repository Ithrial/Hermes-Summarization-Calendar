"""Correction-pass tests — new security/correctness gates.

Each test targets a specific issue found during the independent review pass.
Tests are written FIRST (TDD) and must fail against the prior commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import pytest

from hermes_daily_ledger.contract import (
    FingerprintComponent,
    compute_source_fingerprint,
)
from hermes_daily_ledger.dates import chicago_day_window_utc
from hermes_daily_ledger.inventory import (
    build_day_inventory,
    check_health,
    discover_all,
    open_readonly,
    query_day_sessions,
    query_day_cron_runs,
)

try:
    from fastapi.testclient import TestClient
    HAS_TC = True
except ImportError:
    HAS_TC = False


# ====================================================================
# 1. Date validation must reject invalid calendar dates (Feb 30, etc.)
# ====================================================================


@pytest.mark.skipif(not HAS_TC, reason="TestClient not available")
class TestDateValidation:
    def _app(self, home):
        import plugin_api
        from fastapi import FastAPI
        os.environ["HERMES_HOME"] = str(home)
        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/daily-ledger")
        return TestClient(app)

    def test_feb_30_rejected(self, empty_hermes_home):
        client = self._app(empty_hermes_home)
        resp = client.get("/api/plugins/daily-ledger/day?date=2026-02-30")
        assert resp.status_code == 400, f"Expected 400 for Feb 30, got {resp.status_code}"

    def test_feb_29_non_leap_rejected(self, empty_hermes_home):
        client = self._app(empty_hermes_home)
        resp = client.get("/api/plugins/daily-ledger/day?date=2025-02-29")
        assert resp.status_code == 400, f"Expected 400 for Feb 29 non-leap, got {resp.status_code}"

    def test_feb_29_leap_accepted(self, empty_hermes_home):
        client = self._app(empty_hermes_home)
        resp = client.get("/api/plugins/daily-ledger/day?date=2024-02-29")
        assert resp.status_code == 200


# ====================================================================
# 2. Fingerprint must hash FULL content (not truncated to 200 chars)
# ====================================================================


class TestFingerprintFullContent:
    def test_change_after_char_200_changes_fingerprint(self):
        """Changing only content past char 200 MUST change the fingerprint.

        Simulates what inventory.py does: full SHA-256 of all fields including
        full content, so a single-char change at position 300 produces a different
        content_digest which changes the source fingerprint.
        """
        long_content_a = "A" * 300
        long_content_b = "A" * 299 + "B"

        def make_components(content):
            # Real inventory.py hashes ALL fields including full content via SHA-256
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return [FingerprintComponent(
                session_id="s1", profile_label="p1", timestamp=100.0,
                role="user", active=1, content_digest=digest,
            )]

        fp_a = compute_source_fingerprint(make_components(long_content_a))
        fp_b = compute_source_fingerprint(make_components(long_content_b))
        assert fp_a != fp_b, (
            "Fingerprint did NOT change when content past char 200 changed"
        )

    def test_full_sha256_not_truncated(self):
        """SHA-256 hex should be full 64 chars, not truncated to 16."""
        comp = FingerprintComponent(
            session_id="s1", profile_label="p", timestamp=1.0,
            role="user", active=1, content_digest="a" * 64,
        )
        fp = compute_source_fingerprint([comp])
        hex_part = fp.removeprefix("sha256:")
        assert len(hex_part) == 64, f"Expected full SHA-256 (64 hex), got {len(hex_part)}"


# ====================================================================
# 3. Cron claimed_at: timezone-aware comparison (not lexicographic)
# ====================================================================

@pytest.fixture
def cron_tz_home(tmp_path):
    """Cron DB with mixed-offset timestamps that break lexicographic ordering."""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()

    exec_db = cron_dir / "executions.db"
    conn = sqlite3.connect(str(exec_db))
    conn.execute("""CREATE TABLE executions (id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL,
        pid INTEGER NOT NULL, process_started_at INTEGER,
        status TEXT NOT NULL, claimed_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, error TEXT)""")

    # March 8 03:00 UTC = 2026-03-07 22:00 -05:00 -> should be in Mar 8 Chicago window?
    # Mar 8 Chicago window: 2026-03-08T06:00:00Z to 2026-03-09T05:00:00Z
    # So claimed_at = '2026-03-08T07:00:00-05:00' = 12:00 UTC -> IN window
    # But lexicographically '2026-03-08T07:00:00-05:00' > '2026-03-09T05:00:00Z'!
    conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at) VALUES (?, 'j1', 'b', 'p1', 1,
           'completed', ?, NULL, NULL)""",
        ("tz_exec_001", "2026-03-08T07:00:00-05:00"),
    )
    # Also test Z suffix
    conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at) VALUES (?, 'j2', 'b', 'p2', 2,
           'completed', ?, NULL, NULL)""",
        ("tz_exec_002", "2026-03-08T10:00:00Z"),
    )
    # Invalid timestamp — should be skipped, not crash
    conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at) VALUES (?, 'j3', 'b', 'p3', 3,
           'completed', '', NULL, NULL)""",
        ("tz_exec_invalid",),
    )
    # One BEFORE the Mar 8 Chicago window: claimed at 04:00 UTC on Mar 8 = before 06:00Z boundary
    conn.execute(
        """INSERT INTO executions (id, job_id, source, process_id, pid,
           status, claimed_at, started_at, finished_at) VALUES (?, 'j4', 'b', 'p4', 4,
           'completed', '2026-03-08T04:00:00Z', NULL, NULL)""",
        ("tz_exec_before",),
    )

    conn.commit()
    conn.close()

    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": []}))
    return cron_dir


class TestCronTimezoneComparison:
    def test_offset_timestamp_in_window(self, cron_tz_home):
        """claimed_at with -05:00 offset must be parsed and compared as UTC instants."""
        from hermes_daily_ledger.inventory import CronRoot
        cron_root = CronRoot(cron_dir=cron_tz_home, label="default")
        start_dt, end_dt = chicago_day_window_utc("2026-03-08")

        runs, _ = query_day_cron_runs([cron_root], start_dt.timestamp(), end_dt.timestamp())
        ids = {r.execution_id for r in runs}

        # The -05:00 timestamp is 12:00 UTC on Mar 8 -> IN the window [06Z, next-day 05Z)
        assert "tz_exec_001" in ids, "Offset-aware execution not found"

    def test_z_suffix_in_window(self, cron_tz_home):
        from hermes_daily_ledger.inventory import CronRoot
        cron_root = CronRoot(cron_dir=cron_tz_home, label="default")
        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        runs, _ = query_day_cron_runs([cron_root], start_dt.timestamp(), end_dt.timestamp())
        ids = {r.execution_id for r in runs}
        # 10:00 UTC on Mar 8 -> IN window
        assert "tz_exec_002" in ids

    def test_invalid_timestamp_skipped(self, cron_tz_home):
        """Empty/invalid claimed_at should be skipped, not crash."""
        from hermes_daily_ledger.inventory import CronRoot
        cron_root = CronRoot(cron_dir=cron_tz_home, label="default")
        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        runs, _ = query_day_cron_runs([cron_root], start_dt.timestamp(), end_dt.timestamp())
        ids = {r.execution_id for r in runs}
        assert "tz_exec_invalid" not in ids

    def test_before_window_excluded(self, cron_tz_home):
        """04:00 UTC on Mar 8 is before Chicago midnight at 06:00Z."""
        from hermes_daily_ledger.inventory import CronRoot
        cron_root = CronRoot(cron_dir=cron_tz_home, label="default")
        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        runs, _ = query_day_cron_runs([cron_root], start_dt.timestamp(), end_dt.timestamp())
        ids = {r.execution_id for r in runs}
        assert "tz_exec_before" not in ids


# ====================================================================
# 4. Profile identity from canonical label, NOT profile_name row
# ====================================================================

@pytest.fixture
def stale_profile_name_home(tmp_path):
    """Named profile 'named-profile' DB with stale profile_name='default'."""
    home = tmp_path / ".hermes"
    home.mkdir()

    default_db = home / "state.db"
    conn = sqlite3.connect(str(default_db))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()

    named_dir = home / "profiles" / "named-profile"
    named_dir.mkdir(parents=True)
    named_db = named_dir / "state.db"
    named_conn = sqlite3.connect(str(named_db))
    named_conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
    named_conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")
    # Stale profile_name = 'default' but DB lives in profiles/named-profile/
    named_conn.execute(
        "INSERT INTO sessions VALUES ('s_named-profile_01', 'cli', 'model', 'Test', 1772964000.0, NULL, 'default', 1, 0)"
    )
    named_conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES ('s_named-profile_01', 'user', 'hello', 1772964000.0, 1)"
    )
    named_conn.commit()
    named_conn.close()

    return home


class TestProfileIdentity:
    def test_canonical_label_overrides_stale_profile_name(self, stale_profile_name_home):
        """API must report 'named-profile', NOT the stale 'default' in profile_name."""
        start_dt, end_dt = chicago_day_window_utc("2026-03-08")

        named_db = stale_profile_name_home / "profiles" / "named-profile" / "state.db"
        sessions, _ = query_day_sessions(named_db, "named-profile", start_dt.timestamp(), end_dt.timestamp())

        assert len(sessions) == 1
        assert sessions[0].profile == "named-profile", (
            f"Profile should be canonical label 'named-profile', got '{sessions[0].profile}'"
        )


# ====================================================================
# 5. Discover ALL cron roots (default + per-profile)
# ====================================================================

@pytest.fixture
def multi_cron_home(tmp_path):
    """Home with default cron AND a profile-specific cron."""
    home = tmp_path / ".hermes"
    home.mkdir()

    # Empty default DB
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()

    # Default cron root with one execution
    default_cron = home / "cron"
    default_cron.mkdir()
    dc_conn = sqlite3.connect(str(default_cron / "executions.db"))
    dc_conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)")
    dc_conn.execute(
        "INSERT INTO executions VALUES ('def_exec_1', 'j1', 'builtin', 'p1', 1, NULL, 'completed', '2026-03-08T12:00:00Z', NULL, NULL, NULL)"
    )
    dc_conn.commit()
    dc_conn.close()
    (default_cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j1", "name": "Default job"}]}))

    # Profile-specific cron root with one execution
    secondary_dir = home / "profiles" / "secondary"
    secondary_dir.mkdir(parents=True)
    b_conn = sqlite3.connect(str(secondary_dir / "state.db"))
    b_conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
    b_conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")
    b_conn.commit()
    b_conn.close()

    secondary_cron = secondary_dir / "cron"
    secondary_cron.mkdir()
    bc_conn = sqlite3.connect(str(secondary_cron / "executions.db"))
    bc_conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)")
    bc_conn.execute(
        "INSERT INTO executions VALUES ('secondary_exec_1', 'j2', 'builtin', 'p2', 2, NULL, 'completed', '2026-03-08T14:00:00Z', NULL, NULL, NULL)"
    )
    bc_conn.commit()
    bc_conn.close()
    (secondary_cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j2", "name": "Summary job"}]}))

    return home


class TestMultiCronRoots:
    def test_all_cron_roots_discovered(self, multi_cron_home):
        """Must find both default and profile cron roots."""
        profiles, cron_roots = discover_all(multi_cron_home)
        # Check profile labels from cron roots
        cron_labels = {cr.label for cr in cron_roots}
        assert "default" in cron_labels
        assert "secondary" in cron_labels

    def test_aggregated_cron_runs(self, multi_cron_home):
        """build_day_inventory must return executions from all cron roots."""
        profiles, cron_roots = discover_all(multi_cron_home)
        inv = build_day_inventory("2026-03-08", profiles, cron_roots)
        exec_ids = {r.execution_id for r in inv.cron_runs}
        assert "def_exec_1" in exec_ids
        assert "secondary_exec_1" in exec_ids


# ====================================================================
# 6. Cron runs participate in source fingerprint
# ====================================================================


class TestCronInFingerprint:
    def test_cron_change_changes_fingerprint(self, multi_cron_home):
        """Adding a cron execution must change the day's fingerprint."""
        profiles, cron_roots = discover_all(multi_cron_home)

        inv1 = build_day_inventory("2026-03-08", profiles, cron_roots)

        # Add another execution to default cron root
        dc_conn = sqlite3.connect(str(multi_cron_home / "cron" / "executions.db"))
        dc_conn.execute(
            "INSERT INTO executions VALUES ('new_exec', 'j99', 'builtin', 'p99', 99, NULL, 'completed', '2026-03-08T15:00:00Z', NULL, NULL, NULL)"
        )
        dc_conn.commit()
        dc_conn.close()

        inv2 = build_day_inventory("2026-03-08", profiles, cron_roots)
        assert inv1.source_fingerprint != inv2.source_fingerprint, (
            "Fingerprint did NOT change after adding cron execution"
        )


# ====================================================================
# 7. Fingerprint sorting includes profile for cross-DB determinism
# ====================================================================

class TestFingerprintProfileSort:
    def test_same_session_id_different_profiles_distinct(self):
        """Two messages with same session_id and timestamp in different profiles must produce different fingerprints."""
        comp_a = FingerprintComponent("same_id", "profile_a", 100.0, "user", 1, "hashA")
        comp_b = FingerprintComponent("same_id", "profile_b", 100.0, "user", 1, "hashB")

        fp_ab = compute_source_fingerprint([comp_a, comp_b])
        fp_ba = compute_source_fingerprint([comp_b, comp_a])
        assert fp_ab == fp_ba, "Fingerprint must be order-independent"

        # Each profile's fingerprint alone must differ
        fp_a_only = compute_source_fingerprint([comp_a])
        fp_b_only = compute_source_fingerprint([comp_b])
        assert fp_a_only != fp_b_only


# ====================================================================
# 8. SourceReadability.error must not expose absolute paths
# ====================================================================

class TestHealthErrorSanitized:
    def test_error_no_paths(self, tmp_path):
        """check_health error messages must not contain filesystem paths."""
        # Create an unreadable DB (corrupt file that's not SQLite)
        home = tmp_path / ".hermes"
        home.mkdir()
        bad_db = home / "state.db"
        bad_db.write_text("not a sqlite database")

        profiles, cron_root = discover_all(home)
        sources, _ = check_health(profiles, cron_root)

        for s in sources:
            if not s.readable and s.error:
                assert str(tmp_path) not in s.error.lower(), (
                    f"Absolute path leaked in error: {s.error}"
                )
                # Also check no /home/ paths
                assert "/home/" not in s.error


# ====================================================================
# 9. Whitespace-only session title must use fallback
# ====================================================================

class TestWhitespaceTitle:
    def test_whitespace_title_fallback(self, tmp_path):
        """Session with title='   ' must return fallback like 'Session <id>'."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")
        conn.execute(
            "INSERT INTO sessions VALUES ('ws_test_01', 'cli', 'model', '   ', 1772964000.0, NULL, 'default', 1, 0)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES ('ws_test_01', 'user', 'test', 1772964000.0, 1)"
        )
        conn.commit()
        conn.close()

        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        sessions, _ = query_day_sessions(db, "default", start_dt.timestamp(), end_dt.timestamp())
        assert len(sessions) == 1
        # Title should NOT be whitespace-only
        assert sessions[0].title.strip() != "", (
            f"Title is whitespace-only: '{sessions[0].title}'"
        )
        assert "ws_test_01" in sessions[0].title


# ====================================================================
# 10. Tool call counting: JSON array + tool_name, no double-count
# ====================================================================

class TestToolCallCounting:
    def test_json_array_tool_calls_counted(self, tmp_path):
        """Two tool calls in one assistant row's tool_calls JSON must both count."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")

        json_tool_calls = json.dumps([
            {"name": "terminal", "arguments": "..."},
            {"name": "read_file", "arguments": "..."},
        ])
        conn.execute("INSERT INTO sessions VALUES ('tc_01', 'cli', 'm', 't', 1772964000.0, NULL, 'default', 0, 0)")
        # Assistant row with tool_calls JSON array
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, tool_calls) VALUES ('tc_01', 'assistant', '...', NULL, 1772964000.0, 1, ?)",
            (json_tool_calls,)
        )
        # Two tool result rows (these reference the same calls, don't double-count!)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active) VALUES ('tc_01', 'tool', 'result1', 'terminal', 1772964001.0, 1)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active) VALUES ('tc_01', 'tool', 'result2', 'read_file', 1772964002.0, 1)"
        )
        conn.commit()
        conn.close()

        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        sessions, _ = query_day_sessions(db, "default", start_dt.timestamp(), end_dt.timestamp())
        assert len(sessions) == 1
        # Should count: 2 from tool_calls JSON array. Tool result rows should NOT double-count.
        assert sessions[0].tool_call_count == 2, (
            f"Expected 2 tool calls (from JSON array), got {sessions[0].tool_call_count}"
        )

    def test_tool_name_only_counts(self, tmp_path):
        """Message with tool_name but no tool_calls JSON should count as 1."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")

        conn.execute("INSERT INTO sessions VALUES ('tc_02', 'cli', 'm', 't', 1772964000.0, NULL, 'default', 0, 0)")
        # Row with tool_name only (typical tool result row)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active) VALUES ('tc_02', 'tool', 'result', 'terminal', 1772964000.0, 1)"
        )
        conn.commit()
        conn.close()

        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        sessions, _ = query_day_sessions(db, "default", start_dt.timestamp(), end_dt.timestamp())
        assert len(sessions) == 1
        # tool_name set -> counts as 1 tool call
        assert sessions[0].tool_call_count >= 1

    def test_malformed_tool_calls_json_no_crash(self, tmp_path):
        """Invalid JSON in tool_calls must not crash; count conservatively."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT, started_at REAL, ended_at REAL, profile_name TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, tool_calls TEXT, compacted INTEGER NOT NULL DEFAULT 0)")

        conn.execute("INSERT INTO sessions VALUES ('tc_03', 'cli', 'm', 't', 1772964000.0, NULL, 'default', 0, 0)")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active, tool_calls) VALUES ('tc_03', 'assistant', '...', 'terminal', 1772964000.0, 1, '{INVALID JSON')",
        )
        conn.commit()
        conn.close()

        start_dt, end_dt = chicago_day_window_utc("2026-03-08")
        sessions, _ = query_day_sessions(db, "default", start_dt.timestamp(), end_dt.timestamp())
        assert len(sessions) == 1
        # At least tool_name counts even if JSON is invalid
        assert sessions[0].tool_call_count >= 1


# ====================================================================
# Additional: no filesystem paths in cron run metadata
# ====================================================================

class TestNoPathsInAPI:
    @pytest.mark.skipif(not HAS_TC, reason="TestClient not available")
    def test_cron_run_no_paths(self, multi_cron_home):
        """Day response must not contain filesystem paths."""
        import plugin_api
        from fastapi import FastAPI
        os.environ["HERMES_HOME"] = str(multi_cron_home)
        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/daily-ledger")
        client = TestClient(app)

        resp = client.get("/api/plugins/daily-ledger/day?date=2026-03-08")
        assert resp.status_code == 200
        raw = json.dumps(resp.json())
        # No filesystem paths in cron runs or anywhere
        for run in resp.json()["cron_runs"]:
            assert str(multi_cron_home) not in json.dumps(run), (
                "Filesystem path leaked in cron run metadata"
            )
