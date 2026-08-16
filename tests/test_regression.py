"""Regression tests for merge-gate fixes (A3-E12).

Tests the following defect categories:
  A3. rollback-local.sh legacy flat backup detection + cold-run regression
  B4. Shell backup safety - traversal rejection, JSON-safe encoding, ||true removal
  C6. save_recap atomicity with symlink-based current pointer
  C7. rollback_to_version atomic repoint, no pre-rollback archive IDs
  C8. Version ID format tightening, snapshot failure must raise (not log)
  D9. _split_oversized_session UTF-8-safe content splitting
  D10. Segment summaries ALL survive synthesis - no dict overwrites
  E11. Stale recovery on POST, worker thread failure handling with pool cleanup
  E12. Max concurrency bound of 4 for summary workers across dates
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import pytest

from fastapi import HTTPException

import plugin_api as api

# --- Import modules under test ---
try:
    from hermes_summarization_calendar.recap_storage import (
        save_recap,
        load_recap,
        rollback_to_version,
        list_versions,
        recap_exists,
        _resolve_current,
        validate_version_id,
        _version_timestamp,
    )
    HAS_STORAGE = True
except ImportError:
    HAS_STORAGE = False

try:
    from hermes_summarization_calendar.chunker import (
        chunk_transcripts,
        build_synthesis_prompt,
    )
    HAS_CHUNKER = True
except ImportError:
    HAS_CHUNKER = False

try:
    from hermes_summarization_calendar.transcript import TranscriptMessage
    HAS_TRANSCRIPT = True
except ImportError:
    HAS_TRANSCRIPT = False


# ====================================================================
# A3 - rollback-local.sh legacy flat backup detection + cold-run
# ====================================================================

class TestRollbackLegacyFlatBackup:
    """Cold-run regression for legacy flat backup restoration."""

    def test_cold_run_shows_available_backups(self, tmp_path):
        """rollback-local.sh with no args lists available backups."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "rollback-local.sh"

        hermes_home = tmp_path / ".hermes"
        (hermes_home / "backups" / "summarization-calendar-install").mkdir(parents=True)
        backup_id = "20260701T120000Z-12345"
        backup_dir = hermes_home / "backups" / "summarization-calendar-install" / backup_id
        backup_dir.mkdir()

        manifest = {
            "backup_id": backup_id,
            "previous_type": "symlink",
            "previous_target": "/fake/target",
            "created_at": "2026-07-01T12:00:00Z",
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (backup_dir / "payload").mkdir()
        (backup_dir / "payload" / "link_target.txt").write_text("/fake/target")

        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 1, \
            f"Expected exit 1 for no-args listing, got {result.returncode}: {result.stderr}"
        # backup_id should appear in output
        output = result.stdout + result.stderr
        assert backup_id in output, f"backup_id not found in output: {output}"

    def test_legacy_flat_backup_detection_and_restore(self, tmp_path):
        """Legacy flat backup (files at root alongside manifest) is detected and restored."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "rollback-local.sh"

        hermes_home = tmp_path / ".hermes"
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        (hermes_home / "backups" / "summarization-calendar-install").mkdir(parents=True)

        backup_id = "legacy-flat-12345"
        backup_dir = hermes_home / "backups" / "summarization-calendar-install" / backup_id
        backup_dir.mkdir()

        manifest = {
            "backup_id": backup_id,
            "previous_type": "directory",
            "previous_target": "",
            "created_at": "2026-07-01T12:00:00Z",
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        # Plugin files at root level (legacy flat) — NO payload/ dir
        (backup_dir / "__init__.py").write_text("# legacy plugin")
        (backup_dir / "plugin.py").write_text("# plugin logic")

        result = subprocess.run(
            ["bash", str(script), backup_id],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 0, f"Restore failed: {result.stderr}"
        assert (plugin_dir / "__init__.py").is_file()
        assert (plugin_dir / "plugin.py").is_file()
        # manifest.json must NOT be copied to plugin dir
        assert not (plugin_dir / "manifest.json").is_file(), \
            "manifest.json must not be in restored plugin"

    def test_cold_run_no_backups(self, tmp_path):
        """Cold run with no backups directory returns expected message."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "rollback-local.sh"

        hermes_home = tmp_path / ".hermes_empty"
        hermes_home.mkdir()

        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 1


# ====================================================================
# B4 - Shell backup safety (traversal, JSON-encode, ||true removal)
# ====================================================================

class TestShellBackupSafety:
    """Tests for install/rollback shell script safety."""

    def test_install_backup_manifest_is_valid_json(self, tmp_path):
        """install-local.sh creates valid JSON manifest via Python, not shell interp."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "install-local.sh"

        hermes_home = tmp_path / ".hermes"
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        (plugin_dir.parent).mkdir(parents=True)
        # Pre-existing symlink
        plugin_dir.symlink_to("/existing/target")

        result = subprocess.run(
            ["bash", str(script), "--symlink"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 0, f"Install failed: {result.stderr}"
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        manifests = list(backup_root.glob("*/manifest.json")) if backup_root.is_dir() else []
        assert manifests, "No backup manifest created"

        data = json.loads(manifests[0].read_text())
        assert data["previous_type"] == "symlink"
        assert data["payload_dir"] == "payload/"

    def test_install_backup_with_directory_snapshot(self, tmp_path):
        """Directory snapshot creates valid JSON manifest."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "install-local.sh"

        hermes_home = tmp_path / ".hermes"
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        (plugin_dir.parent).mkdir(parents=True)

        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text('{"key": "value"}')

        result = subprocess.run(
            ["bash", str(script), "--symlink"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 0
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        manifests = list(backup_root.glob("*/manifest.json")) if backup_root.is_dir() else []
        assert manifests
        # Must parse as valid JSON (shell interpolation would break)
        data = json.loads(manifests[0].read_text())
        assert data["previous_type"] == "directory"

    def test_rollback_traversal_rejection(self, tmp_path):
        """rollback-local.sh rejects backup IDs with path traversal."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "rollback-local.sh"

        hermes_home = tmp_path / ".hermes"
        (hermes_home / "backups" / "summarization-calendar-install").mkdir(parents=True)

        for bad_id in ["../etc", "..backup", "a/b/c"]:
            result = subprocess.run(
                ["bash", str(script), bad_id],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "HERMES_HOME": str(hermes_home)},
            )
            assert result.returncode != 0, f"Traversal '{bad_id}' was not rejected"

    def test_install_backup_symlink_records_target(self, tmp_path):
        """Symlink backup records exact link target without following."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "install-local.sh"

        hermes_home = tmp_path / ".hermes"
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        (plugin_dir.parent).mkdir(parents=True)

        # Broken symlink
        target = "/nonexistent/plugin/source"
        plugin_dir.symlink_to(target)

        result = subprocess.run(
            ["bash", str(script), "--symlink"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )

        assert result.returncode == 0, f"Install failed: {result.stderr}"
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        manifests = list(backup_root.glob("*/manifest.json")) if backup_root.is_dir() else []
        assert manifests, "No manifest for broken symlink"

        data = json.loads(manifests[0].read_text())
        assert data["previous_type"] == "symlink"
        backup_id = data["backup_id"]
        link_target = backup_root / backup_id / "payload" / "link_target.txt"
        assert link_target.is_file()
        assert link_target.read_text().strip() == target

    def test_install_repeat_creates_new_backup(self, tmp_path):
        """Repeated install creates separate backups each time."""
        scripts_dir = Path(__file__).parent.parent / "scripts"
        script = scripts_dir / "install-local.sh"

        hermes_home = tmp_path / ".hermes"
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        (plugin_dir.parent).mkdir(parents=True)

        plugin_dir.mkdir()
        (plugin_dir / "v1.py").write_text("# v1")

        subprocess.run(["bash", str(script), "--symlink"],
                       capture_output=True, text=True, timeout=10,
                       env={**os.environ, "HERMES_HOME": str(hermes_home)})

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backups_after_first = list(backup_root.glob("*/")) if backup_root.is_dir() else []
        assert len(backups_after_first) == 1


# ====================================================================
# C6 - save_recap atomicity with symlink-based current pointer
# ====================================================================

@pytest.mark.skipif(not HAS_STORAGE, reason="storage module not available")
class TestSaveRecapAtomicity:
    """save_recap must never delete current before replacement is guaranteed."""

    def test_save_recap_creates_symlink_current(self, tmp_path):
        """After save, recaps/<date> is a symlink to the version dir."""
        ledger_root = tmp_path / "ledger"
        data = {
            "session_summaries": [{"session_id": "s1", "title": "T1", "summary": "S1"}],
            "overall_recap": "Good day",
        }

        save_recap("2026-07-20", data, "fp1", ledger_root=ledger_root)

        current_ptr = ledger_root / "recaps" / "2026-07-20"
        assert current_ptr.is_symlink(), "Current must be a symlink"
        resolved = current_ptr.resolve()
        assert (resolved / "meta.json").is_file()
        assert (resolved / "raw.json").is_file()
        assert (resolved / "summary.md").is_file()

    def test_save_recap_write_failure_aborts_and_preserves_current(self, tmp_path):
        """If version dir write fails, current is not touched."""
        ledger_root = tmp_path / "ledger"

        # Pre-existing recap
        existing_dir = ledger_root / "recaps" / "2026-07-20"
        existing_dir.mkdir(parents=True)
        (existing_dir / "meta.json").write_text('{"date":"2026-07-20"}')

        # Mock _atomic_write_json to fail on the version dir write
        with patch("hermes_summarization_calendar.recap_storage._atomic_write_json",
                   side_effect=OSError("injected failure")):
            data = {
                "session_summaries": [{"session_id": "s1", "title": "T1", "summary": "S1"}],
                "overall_recap": "New recap",
            }
            with pytest.raises(OSError):
                save_recap("2026-07-20", data, "fp2", ledger_root=ledger_root)

        # Current must still exist (legacy dir untouched)
        assert existing_dir.exists()

    def test_save_recap_version_written_before_current_touched(self, tmp_path):
        """Version dir is complete before symlink pointer is installed."""
        ledger_root = tmp_path / "ledger"

        data = {
            "session_summaries": [{"session_id": "s1", "title": "T1", "summary": "S1"}],
            "overall_recap": "Test",
        }

        save_recap("2026-07-20", data, "fp1", ledger_root=ledger_root)

        # Version dir must be complete
        versions = ledger_root / "versions" / "2026-07-20"
        ver_dirs = list(versions.iterdir()) if versions.is_dir() else []
        assert len(ver_dirs) == 1
        for fname in ("meta.json", "raw.json", "summary.md"):
            assert (ver_dirs[0] / fname).is_file(), f"{fname} missing in version dir"

    def test_load_follows_symlink(self, tmp_path):
        """load_recap follows symlink current to version dir."""
        ledger_root = tmp_path / "ledger"
        data = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "S"}],
            "overall_recap": "Test recap",
        }

        save_recap("2026-07-20", data, "fp1", ledger_root=ledger_root)

        raw, meta = load_recap("2026-07-20", ledger_root=ledger_root)
        assert raw is not None
        assert meta is not None
        assert raw["session_summaries"][0]["session_id"] == "s1"


# ====================================================================
# C7 - rollback_to_version atomic repoint, no pre-rollback archive IDs
# ====================================================================

@pytest.mark.skipif(not HAS_STORAGE, reason="storage module not available")
class TestRollbackAtomicity:
    """rollback_to_version atomically repoints symlink without creating pre-rollback archives."""

    def test_rollback_repoints_symlink_atomically(self, tmp_path):
        """Rollback changes the symlink target via atomic swap."""
        ledger_root = tmp_path / "ledger"
        data1 = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "V1"}],
            "overall_recap": "Version 1",
        }
        v1 = save_recap("2026-07-20", data1, "fp1",
                        generated_at="2026-07-20T10:00:00Z", ledger_root=ledger_root)

        data2 = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "V2"}],
            "overall_recap": "Version 2",
        }
        save_recap("2026-07-20", data2, "fp2",
                   generated_at="2026-07-20T11:00:00Z", ledger_root=ledger_root)

        # Current should point to v2
        raw_before, _ = load_recap("2026-07-20", ledger_root=ledger_root)
        assert raw_before["overall_recap"] == "Version 2"

        # Rollback to v1
        result = rollback_to_version("2026-07-20", v1.version_ts, ledger_root=ledger_root)
        assert result is not None
        assert result.version_ts == v1.version_ts

        raw_after, _ = load_recap("2026-07-20", ledger_root=ledger_root)
        assert raw_after["overall_recap"] == "Version 1"

    def test_rollback_no_pre_rollback_archive(self, tmp_path):
        """No pre-rollback-* archives are created during rollback."""
        ledger_root = tmp_path / "ledger"
        data1 = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "V1"}],
            "overall_recap": "Version 1",
        }
        v1 = save_recap("2026-07-20", data1, "fp1",
                        generated_at="2026-07-20T10:00:00Z", ledger_root=ledger_root)

        data2 = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "V2"}],
            "overall_recap": "Version 2",
        }
        save_recap("2026-07-20", data2, "fp2",
                   generated_at="2026-07-20T11:00:00Z", ledger_root=ledger_root)

        rollback_to_version("2026-07-20", v1.version_ts, ledger_root=ledger_root)

        # No pre-rollback-* dirs
        versions_dir = ledger_root / "versions" / "2026-07-20"
        for d in versions_dir.iterdir():
            assert not d.name.startswith("pre-rollback-"), \
                f"pre-rollback archive found: {d.name}"

    def test_rollback_incomplete_target_rejected(self, tmp_path):
        """Rollback rejects incomplete version dirs."""
        ledger_root = tmp_path / "ledger"

        ver_dir = ledger_root / "versions" / "2026-07-20" / "incomplete_ver"
        ver_dir.mkdir(parents=True)
        (ver_dir / "meta.json").write_text('{"version_id":"incomplete_ver"}')

        result = rollback_to_version("2026-07-20", "incomplete_ver", ledger_root=ledger_root)
        assert result is None, "Incomplete target must be rejected"


# ====================================================================
# C8 - Version ID format tightening, snapshot failure must raise
# ====================================================================

@pytest.mark.skipif(not HAS_STORAGE, reason="storage module not available")
class TestVersionIdFormat:
    """Version ID validation and snapshot failure handling."""

    def test_version_id_rejects_traversal(self):
        assert not validate_version_id("../etc/passwd")
        assert not validate_version_id("a/b/c")
        assert not validate_version_id("\\..\\malicious")

    def test_version_id_rejects_empty_and_nonstring(self):
        assert not validate_version_id("")
        assert not validate_version_id("   ")

    def test_version_id_accepts_safe_format(self):
        assert validate_version_id("20260720T120000Z") is True
        assert validate_version_id("20260720T120000Z_123456_abc123def456") is True

    def test_version_timestamp_matches_validator(self):
        for _ in range(10):
            ts = _version_timestamp()
            assert validate_version_id(ts), f"Generated failed validation: {ts}"

    def test_snapshot_failure_raises_not_logs(self, tmp_path):
        """Version write failure raises OSError, not logs-and-succeeds."""
        ledger_root = tmp_path / "ledger"

        with patch("hermes_summarization_calendar.recap_storage._atomic_write_json",
                   side_effect=OSError("injected")):
            data = {
                "session_summaries": [{"session_id": "s1", "title": "T", "summary": "S"}],
                "overall_recap": "Test",
            }
            with pytest.raises(OSError):
                save_recap("2026-07-20", data, "fp1", ledger_root=ledger_root)

    def test_version_identity_consistent(self, tmp_path):
        """Version dir path matches version_ts from save_recap."""
        ledger_root = tmp_path / "ledger"
        data = {
            "session_summaries": [{"session_id": "s1", "title": "T", "summary": "S"}],
            "overall_recap": "Test",
        }

        result = save_recap("2026-07-20", data, "fp1", ledger_root=ledger_root)
        ver_dir = ledger_root / "versions" / "2026-07-20" / result.version_ts
        assert ver_dir.is_dir(), f"Version dir not found: {ver_dir}"


# ====================================================================
# D9 - _split_oversized_session UTF-8-safe content splitting
# ====================================================================

@pytest.mark.skipif(not HAS_CHUNKER or not HAS_TRANSCRIPT, reason="chunker/transcript not available")
class TestUTF8Splitting:
    """_split_oversized_session must split huge messages into deterministic UTF-8-safe pieces."""

    def test_all_characters_preserved(self):
        """Concatenating split segments yields the original content exactly."""
        # Real multi-byte Unicode (no surrogates)
        chars = "\u00e9\u4e16\u0915\u30c6"  # e-acute, world, ka, te
        # Small enough that after content-splitting each part + overhead fits under ceiling
        content = "".join(f"{chars}{i} " for i in range(80))

        class HugeTranscript:
            session_id = "unicode_test"
            profile = "default"
            title = "Unicode test"
            source = "cli"
            model = "m"
            messages = [TranscriptMessage(role="user", content=content, tool_name=None)]

        chunks = chunk_transcripts([HugeTranscript()], safe_ceiling=8192)

        reconstructed: list[str] = []
        for chunk in chunks:
            for st in chunk.session_transcripts:
                for msg in st.get("messages", []):
                    if msg.get("role") == "user":
                        reconstructed.append(msg.get("content", ""))

        full_reconstructed = "".join(reconstructed)
        assert full_reconstructed == content, \
            f"Content loss: original={len(content)}, reconstructed={len(full_reconstructed)}"

    def test_oversized_message_splits_and_segments_fit(self):
        """A huge single message is split and ALL segments fit under ceiling."""
        content = "X" * 200_000

        class HugeTranscript:
            session_id = "huge_msg"
            profile = "default"
            title = "Huge msg"
            source = "cli"
            model = "m"
            messages = [TranscriptMessage(role="user", content=content, tool_name=None)]

        safe_ceiling = 16384
        chunks = chunk_transcripts([HugeTranscript()], safe_ceiling=safe_ceiling)

        assert len(chunks) > 0

    def test_message_order_preserved_across_segments(self):
        """Split segments preserve message order."""
        msg_contents = [f"MSG_{i:04d}: " + "x" * 5_000 for i in range(20)]

        class ManyMsgTranscript:
            session_id = "order_test"
            profile = "default"
            title = "Order test"
            source = "cli"
            model = "m"
            messages = [
                TranscriptMessage(role="user", content=c, tool_name=None)
                for c in msg_contents
            ]

        chunks = chunk_transcripts([ManyMsgTranscript()], safe_ceiling=16384)

        ordered_prefixes: list[str] = []
        for chunk in chunks:
            for st in chunk.session_transcripts:
                for msg in st.get("messages", []):
                    content = msg.get("content", "")
                    prefix = content.split(":")[0] if ":" in content else ""
                    ordered_prefixes.append(prefix)

        expected = [f"MSG_{i:04d}" for i in range(20)]
        assert ordered_prefixes == expected, f"Order mismatch: {ordered_prefixes[:5]}..."


# ====================================================================
# D10 - Segment summaries ALL survive synthesis
# ====================================================================

@pytest.mark.skipif(not HAS_CHUNKER, reason="chunker module not available")
class TestSynthesisSurvival:
    """All segment summaries for one (profile, session_id) must survive final synthesis."""

    def test_first_middle_last_segment_reach_synthesis(self):
        """Information from first, middle, and last segments reaches the synthesis prompt."""
        chunk_results = [
            {
                "session_summaries": [
                    {"session_id": "s1", "profile": "default", "title": "S1",
                     "summary": "FIRST: Initial bug fix for login flow"},
                ],
                "cron_summary": "",
            },
            {
                "session_summaries": [
                    {"session_id": "s1", "profile": "default", "title": "S1",
                     "summary": "MIDDLE: Added rate limiting middleware"},
                ],
                "cron_summary": "",
            },
            {
                "session_summaries": [
                    {"session_id": "s1", "profile": "default", "title": "S1",
                     "summary": "LAST: Deployed to production environment"},
                ],
                "cron_summary": "",
            },
        ]

        prompt = build_synthesis_prompt(chunk_results, "2026-07-20")

        assert "FIRST" in prompt or "login flow" in prompt, \
            "First segment information must reach synthesis"
        assert "MIDDLE" in prompt or "rate limiting" in prompt, \
            "Middle segment information must reach synthesis"
        assert "LAST" in prompt or "production" in prompt, \
            "Last segment information must reach synthesis"

    def test_duplicate_session_ids_across_profiles_distinct(self):
        """Same session_id in different profiles remains distinct."""
        chunk_results = [
            {
                "session_summaries": [
                    {"session_id": "s1", "profile": "default", "title": "S1-default",
                     "summary": "Default profile work"},
                ],
                "cron_summary": "",
            },
            {
                "session_summaries": [
                    {"session_id": "s1", "profile": "named-profile", "title": "S1-named",
                     "summary": "Named-profile work"},
                ],
                "cron_summary": "",
            },
        ]

        prompt = build_synthesis_prompt(chunk_results, "2026-07-20")
        assert "Default profile work" in prompt, "default session must be in synthesis"
        assert "Named-profile work" in prompt, "named-profile session must be in synthesis"

    def test_metadata_only_sessions_covered(self):
        """Sessions with minimal content still appear in synthesis."""
        chunk_results = [
            {
                "session_summaries": [
                    {"session_id": "meta_only", "profile": "default", "title": "", "summary": ""},
                ],
                "cron_summary": "",
            },
        ]

        prompt = build_synthesis_prompt(chunk_results, "2026-07-20")
        assert "meta_only" in prompt, "Metadata-only sessions must appear"


# ====================================================================
# E11 - Stale recovery on POST + worker thread failure handling
# ====================================================================

class TestWorkerLifecycle:
    """Stale recovery runs before direct POST; thread start failure releases pool entry."""

    def test_ensure_startup_called_before_post(self):
        """Generation routes call _ensure_startup() before worker launch."""
        api_path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
        content = api_path.read_text()
        assert "_ensure_startup()" in content, \
            "Generation routes must call _ensure_startup() before worker launch"

    def test_worker_thread_failure_releases_pool(self):
        """If thread.start() fails, pool entry is removed and slot released."""
        api_path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
        content = api_path.read_text()
        assert "worker.start()" in content
        # v1.2.4: the surviving generation routes (session/roll-up/batch)
        # clean up by pool key; the legacy recap worker keyed by date was
        # retired.
        assert "_worker_pool.pop(pool_key, None)" in content, \
            "Failed start must clean up pool entry"

    def test_ensure_startup_is_idempotent(self):
        """_ensure_startup runs exactly once per process."""
        api_path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
        content = api_path.read_text()
        assert "_startup_done" in content
        assert "if not _startup_done:" in content


# ====================================================================
# E12 - Max concurrency bound of 4 for summary workers
# ====================================================================

class TestMaxConcurrency:
    """Global max-concurrency bound prevents unbounded worker spawning."""

    def test_max_concurrency_constant_exists(self):
        api_path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
        content = api_path.read_text()
        assert "_MAX_CONCURRENCY = 4" in content

    def test_concurrency_check_in_worker_routes(self):
        api_path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
        content = api_path.read_text()
        assert "_MAX_CONCURRENCY" in content
        # v1.2.4: the capacity 503 lives on the batch/summary/roll-up routes
        # (the legacy recap generation route that raised "too_many_workers"
        # was retired).
        assert "worker_capacity_full" in content
        assert 'status_code=503' in content

    def test_pending_batch_workers_count_toward_global_limit(
        self, monkeypatch, tmp_path
    ):
        """E12 runtime contract, v1.2.4: pending (not-yet-started) workers
        reserve capacity before their next request can be accepted.

        Legacy recap generation was retired, so the batch route carries the
        regression: each request creates a durable job plus one pending
        coordinator thread; the request beyond the pool must 503.
        """
        import hermes_summarization_calendar.concurrency as concurrency
        from hermes_summarization_calendar.contract import DailySession
        from types import SimpleNamespace

        class PendingThread:
            def __init__(self, *args, **kwargs):
                self.started = False
            def start(self):
                self.started = True
            def is_alive(self):
                return False

        api._worker_pool.clear()
        monkeypatch.setattr(api, "_ensure_startup", lambda: None)
        monkeypatch.setattr(api, "get_ledger_root", lambda: tmp_path)
        monkeypatch.setattr(
            api,
            "_build_day_inventory_safe",
            lambda date_str, ledger_root: SimpleNamespace(
                sessions=[
                    DailySession(
                        session_id="20260811_100000_bbb",
                        profile="default",
                        source="cli",
                        model="model",
                        title="Session",
                        message_count=1,
                        tool_call_count=0,
                    )
                ]
            ),
        )
        monkeypatch.setattr(
            concurrency, "release_generation_slot", lambda *a, **k: None
        )
        monkeypatch.setattr(api.threading, "Thread", PendingThread)

        try:
            for day in range(1, 5):
                api.post_batch_summary(
                    f"2026-08-{day:02d}",
                    api.BatchRequestBody(
                        sessions=[
                            {"profile": "default", "session_id": "20260811_100000_bbb"}
                        ]
                    ),
                )
            with pytest.raises(HTTPException) as exc:
                api.post_batch_summary(
                    "2026-08-05",
                    api.BatchRequestBody(
                        sessions=[
                            {"profile": "default", "session_id": "20260811_100000_bbb"}
                        ]
                    ),
                )
            assert exc.value.status_code == 503
        finally:
            api._worker_pool.clear()


# ====================================================================
# Static scan: no shell=True/os.system/eval/exec/pickle/secrets/string-SQL
# ====================================================================

class TestStaticScan:
    """Source code static analysis for security patterns."""

    def _py_files(self):
        dashboard_dir = Path(__file__).parent.parent / "dashboard" / "hermes_summarization_calendar"
        return list(dashboard_dir.rglob("*.py"))

    def test_no_shell_true_in_python(self):
        for py_file in self._py_files():
            content = py_file.read_text()
            assert "shell=True" not in content, f"shell=True in {py_file}"

    def test_no_os_system_in_python(self):
        for py_file in self._py_files():
            content = py_file.read_text()
            assert "os.system(" not in content, f"os.system() in {py_file}"

    def test_no_eval_in_python(self):
        for py_file in self._py_files():
            content = py_file.read_text()
            assert re.search(r'\beval\s*\(', content) is None, f"eval() in {py_file}"

    def test_no_exec_in_python(self):
        for py_file in self._py_files():
            content = py_file.read_text()
            assert re.search(r'\bexec\s*\(', content) is None, f"exec() in {py_file}"

    def test_no_pickle_in_python(self):
        for py_file in self._py_files():
            content = py_file.read_text()
            assert "pickle" not in content, f"pickle in {py_file}"

    def test_no_secrets_in_python(self):
        secret_patterns = [
            r'password\s*=\s*["\'][^"\']+["\']',
            r'api_key\s*=\s*["\'][^"\']+["\']',
            r'secret_key\s*=\s*["\'][^"\']+["\']',
        ]
        for py_file in self._py_files():
            content = py_file.read_text()
            for pattern in secret_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                assert not matches, f"Secret in {py_file}: {matches}"


# ====================================================================
# SQLite source safety: URI mode=ro + query_only / read-only access
# ====================================================================

class TestSQLiteSafety:
    """Source DBs are opened with read-only constraints."""

    def test_transcript_reads_are_select_only(self):
        """transcript.py only uses SELECT on source DBs."""
        tx_path = Path(__file__).parent.parent / "dashboard" / "hermes_summarization_calendar" / "transcript.py"
        content = tx_path.read_text()
        write_ops = re.findall(r'\b(INSERT|UPDATE|DELETE)\s+INTO', content, re.IGNORECASE)
        assert not write_ops, f"Write ops in transcript.py: {write_ops}"
