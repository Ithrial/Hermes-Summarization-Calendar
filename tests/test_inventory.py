"""Tests for read-only inventory: profile discovery, RO safety, queries, fingerprinting."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import pytest

from hermes_summarization_calendar.contract import (
    FingerprintComponent,
    _safe_job_name,
    compute_source_fingerprint,
)
from hermes_summarization_calendar.inventory import (
    discover_all,
    discover_cron_roots,
    discover_profiles,
    get_cron_root,
    open_readonly,
    query_day_sessions,
    query_day_cron_runs,
    build_month_inventory,
    build_day_inventory,
    check_health,
)


class TestOpenReadonly:
    def test_readonly_refuses_insert(self, tmp_path: Path):
        """Opening with mode=ro + query_only must reject writes."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        ro = open_readonly(db_path)
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("INSERT INTO t VALUES (2)")
        ro.close()

    def test_readonly_refuses_create_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        ro = open_readonly(db_path)
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("CREATE TABLE u (y TEXT)")
        ro.close()

    def test_readonly_refuses_alter_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        ro = open_readonly(db_path)
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("ALTER TABLE t ADD COLUMN z TEXT")
        ro.close()

    def test_readonly_refuses_drop(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        ro = open_readonly(db_path)
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute("DROP TABLE t")
        ro.close()

    def test_readonly_refuses_prAGMA_journal_mode(self, tmp_path: Path):
        """PRAGMA journal_mode in write mode must be rejected."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        ro = open_readonly(db_path)
        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro.execute("PRAGMA journal_mode=WAL")
        ro.close()

    def test_readonly_does_not_modify_file(self, tmp_path: Path):
        """Opening read-only must not change file hash or mtime."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        original_mtime = db_path.stat().st_mtime
        original_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

        ro = open_readonly(db_path)
        row = ro.execute("SELECT x FROM t").fetchone()[0]
        assert row == 42
        ro.close()

        after_mtime = db_path.stat().st_mtime
        after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

        assert original_mtime == after_mtime, "mtime changed on read-only open"
        assert original_hash == after_hash, "file hash changed on read-only open"


class TestProfileDiscovery:
    def test_discovers_default_and_named(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles = discover_profiles(home)
        labels = [p.label for p in profiles]
        assert "default" in labels
        assert "named-profile" in labels

    def test_no_extra_profiles(self, test_hermes_home):
        """Should not find phantom profiles."""
        home, _ = test_hermes_home
        profiles = discover_profiles(home)
        # Only default + named-profile created
        assert len(profiles) == 2

    def test_cron_root_discovered(self, test_hermes_home):
        home, _ = test_hermes_home
        cron_roots = discover_cron_roots(home)
        assert len(cron_roots) >= 1
        assert any((cr.cron_dir / "executions.db").is_file() for cr in cron_roots)

    def test_empty_home(self, empty_hermes_home):
        profiles = discover_profiles(empty_hermes_home)
        # Default exists but is empty
        assert len(profiles) == 1
        assert profiles[0].label == "default"

    def test_cron_root_none_when_missing(self, empty_hermes_home):
        cron = get_cron_root(empty_hermes_home)
        assert cron is None


class TestDaySessionQuery:
    def test_finds_sessions_on_march_8(self, test_hermes_home):
        """March 8 sessions should include s2, s4 (excluded), s5 (inactive), s6, leo."""
        home, _ = test_hermes_home
        default_db = home / "state.db"

        # March 8 Chicago day window: 2026-03-08T06:00:00Z to 2026-03-09T05:00:00Z (23h)
        from hermes_summarization_calendar.dates import chicago_day_window_utc
        start, end = chicago_day_window_utc("2026-03-08")

        sessions, _ = query_day_sessions(default_db, "default", start.timestamp(), end.timestamp())
        session_ids = {s.session_id for s in sessions}

        # s2 and s6 should be found (active messages in window)
        assert "20260308_100000_bbb" in session_ids
        assert "20260308_tool_ff" in session_ids

        # Plugin-internal session (source=summarization-calendar) should be excluded
        assert "20260308_recap_dd" not in session_ids

        # Legacy plugin-internal session (source=daily-ledger, pre-rename) excluded
        assert "20260308_recap_legacy" not in session_ids

        # Inactive message session (active=0) should NOT appear
        assert "20260308_inactive_ee" not in session_ids

    def test_tool_source_not_excluded(self, test_hermes_home):
        """Sessions with source='tool' must NOT be excluded."""
        home, _ = test_hermes_home
        default_db = home / "state.db"
        from hermes_summarization_calendar.dates import chicago_day_window_utc

        start, end = chicago_day_window_utc("2026-03-08")
        sessions, _ = query_day_sessions(default_db, "default", start.timestamp(), end.timestamp())
        ids = [s.session_id for s in sessions]
        assert "20260308_tool_ff" in ids

    def test_multi_profile_sessions(self, test_hermes_home):
        """Sessions from both default and named-profile profiles on March 8."""
        home, _ = test_hermes_home
        from hermes_summarization_calendar.dates import chicago_day_window_utc

        start, end = chicago_day_window_utc("2026-03-08")

        # Default profile sessions
        default_sessions, _ = query_day_sessions(
            home / "state.db", "default", start.timestamp(), end.timestamp()
        )
        assert len(default_sessions) >= 1

        # Named profile sessions
        named_sessions, _ = query_day_sessions(
            home / "profiles" / "named-profile" / "state.db",
            "named-profile",
            start.timestamp(),
            end.timestamp(),
        )
        assert len(named_sessions) == 1
        assert named_sessions[0].session_id == "named_20260308_001"


class TestCronQuery:
    def test_finds_cron_on_march_8(self, test_hermes_home):
        home, _ = test_hermes_home
        _, cron_roots = discover_all(home)

        from hermes_summarization_calendar.dates import chicago_day_window_utc
        start, end = chicago_day_window_utc("2026-03-08")

        runs, _ = query_day_cron_runs(cron_roots, start.timestamp(), end.timestamp())
        assert len(runs) >= 1  # At least one execution in window

    def test_cron_job_name_from_jobs_json(self, test_hermes_home):
        """Job names come from jobs.json, not embedded."""
        home, _ = test_hermes_home
        _, cron_roots = discover_all(home)

        from hermes_summarization_calendar.dates import chicago_day_window_utc
        start, end = chicago_day_window_utc("2026-03-08")

        runs, _ = query_day_cron_runs(cron_roots, start.timestamp(), end.timestamp())
        by_id = {r.job_id: r for r in runs}
        # One of the jobs should have a name from jobs.json
        found_name = any(
            r.job_name != r.job_id for r in runs if r.status == "completed"
        )
        assert found_name, "No job resolved to its JSON name"

    def test_cron_error_sanitized(self, test_hermes_home):
        """Error text should have PIDs and paths redacted."""
        home, _ = test_hermes_home
        _, cron_roots = discover_all(home)

        from hermes_summarization_calendar.dates import chicago_day_window_utc
        start, end = chicago_day_window_utc("2026-03-08")

        runs, _ = query_day_cron_runs(cron_roots, start.timestamp(), end.timestamp())
        failed_run = next((r for r in runs if r.error_summary is not None), None)
        # If there's an error_summary, it should be sanitized
        if failed_run is not None:
            assert "99999" not in (failed_run.error_summary or "")
            assert "/home/alice/" not in (failed_run.error_summary or "")

    def test_no_cron_when_root_missing(self):
        runs, _ = query_day_cron_runs(None, 0.0, 100.0)
        assert runs == []


class TestFingerprinting:
    def test_deterministic_order(self):
        """Fingerprint must be stable regardless of input order."""
        comp1 = FingerprintComponent("s1", "default", 100.0, "user", 1, "abc")
        comp2 = FingerprintComponent("s2", "default", 200.0, "assistant", 1, "def")

        fp_a = compute_source_fingerprint([comp1, comp2])
        fp_b = compute_source_fingerprint([comp2, comp1])
        assert fp_a == fp_b

    def test_changes_when_activity_changes(self):
        """Different timestamps produce different fingerprints."""
        c1 = FingerprintComponent("s1", "default", 100.0, "user", 1, "abc")
        c2 = FingerprintComponent("s1", "default", 200.0, "user", 1, "abc")

        assert compute_source_fingerprint([c1]) != compute_source_fingerprint([c2])

    def test_empty_list(self):
        fp = compute_source_fingerprint([])
        assert isinstance(fp, str) and fp.startswith("sha256:")

    def test_content_digest_changes(self):
        c1 = FingerprintComponent("s1", "p", 100.0, "user", 1, "hashA")
        c2 = FingerprintComponent("s1", "p", 100.0, "user", 1, "hashB")
        assert compute_source_fingerprint([c1]) != compute_source_fingerprint([c2])


class TestSafeJobName:
    def test_valid_name(self):
        assert _safe_job_name("jid1", "My Job") == "My Job"

    def test_none_fallback(self):
        assert _safe_job_name("jid1", None) == "jid1"

    def test_empty_fallback(self):
        assert _safe_job_name("jid1", "") == "jid1"

    def test_whitespace_fallback(self):
        assert _safe_job_name("jid1", "  ") == "jid1"


class TestBuildMonthInventory:
    def test_march_2026_has_activity(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        month_inv = build_month_inventory(2026, 3, profiles, cron_roots)
        assert month_inv.year == 2026
        assert month_inv.month == 3
        assert len(month_inv.days) == 31  # March has 31 days

        # March 8 should be active (sessions + cron runs)
        march_8_cell = next((d for d in month_inv.days if d.date == "2026-03-08"), None)
        assert march_8_cell is not None
        assert march_8_cell.active is True
        assert march_8_cell.session_count >= 1

    def test_empty_month(self, empty_hermes_home):
        profiles, cron_roots = discover_all(empty_hermes_home)
        inv = build_month_inventory(2026, 12, profiles, cron_roots)
        for day in inv.days:
            assert day.active is False


class TestBuildDayInventory:
    def test_session_metadata(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        inv = build_day_inventory("2026-03-08", profiles, cron_roots)
        assert inv.date == "2026-03-08"
        assert len(inv.sessions) >= 1
        # Check no raw content in response
        for s in inv.sessions:
            assert not hasattr(s, "content")
            assert not hasattr(s, "system_prompt")

    def test_source_fingerprint_present(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        inv = build_day_inventory("2026-03-08", profiles, cron_roots)
        assert inv.source_fingerprint.startswith("sha256:")
        assert all(s.source_fingerprint.startswith("sha256:") for s in inv.sessions)

    def test_session_fingerprint_isolated_to_changed_session(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        before = build_day_inventory("2026-03-08", profiles, cron_roots)
        before_by_id = {
            (session.profile, session.session_id): session.source_fingerprint
            for session in before.sessions
        }
        target = ("default", "20260308_100000_bbb")
        control = ("default", "20260308_tool_ff")
        assert before_by_id[target] != before_by_id[control]

        conn = sqlite3.connect(str(home / "state.db"))
        conn.execute(
            "UPDATE messages SET content = ? WHERE session_id = ?",
            ("changed only in target session", target[1]),
        )
        conn.commit()
        conn.close()

        after = build_day_inventory("2026-03-08", profiles, cron_roots)
        after_by_id = {
            (session.profile, session.session_id): session.source_fingerprint
            for session in after.sessions
        }
        assert after_by_id[target] != before_by_id[target]
        assert after_by_id[control] == before_by_id[control]


class TestHealth:
    def test_health_ok(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        sources, cron_readable = check_health(profiles, cron_roots)
        assert len(sources) == 2  # default + named-profile
        readable = [s for s in sources if s.readable]
        assert len(readable) == 2
        assert cron_readable is True

    def test_no_absolute_paths(self, test_hermes_home):
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)
        sources, _ = check_health(profiles, cron_roots)
        for s in sources:
            assert str(home) not in (s.error or "")
            # profile_label should just be the name, not a path
            assert "/" not in s.profile_label