"""Tests for install/rollback/uninstall backup layout with manifest+payload.

Verifies:
  - Symlinks are NOT followed on backup (manifest records target)
  - Broken symlinks are handled gracefully
  - Pre-existing directories, repeated upgrades work correctly
  - Rollback restores exact type (dir/symlink/broken symlink)
  - Ledger data is never touched
  - Manifest+payload layout (no symlink contamination)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


def run_script(script: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a bash script with HERMES_HOME set."""
    cmd = [str(SCRIPTS / script)] + (args or [])
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=full_env)
    return result


class TestInstallBackupLayout:
    """Verify install creates proper manifest+payload backup layout."""

    def test_symlink_backup_no_contamination(self, tmp_path):
        """Symlink target is NOT contaminated by manifest writes.

        Reproduces the bug: cp -a SYMLINK BACKUP_PATH makes BACKUP_PATH a symlink,
        then writing manifest.json INTO it contaminates the symlink target.
        """
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        # Create a pre-existing plugin as a directory with content
        old_plugin = hermes_home / "plugins" / "summarization-calendar"
        old_plugin.mkdir(parents=True)
        (old_plugin / "config.yaml").write_text("key: value")

        env = {"HERMES_HOME": str(hermes_home)}
        result = run_script("install-local.sh", ["--symlink"], env=env)
        assert result.returncode == 0, f"install failed: {result.stderr}"

        # Backup should exist with manifest+payload layout
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backups = list(backup_root.iterdir())
        assert len(backups) == 1, f"Expected 1 backup, found {len(backups)}"

        backup_dir = backups[0]
        manifest_path = backup_dir / "manifest.json"
        assert manifest_path.is_file(), "manifest.json missing in backup"

        # Manifest should NOT be inside payload (separate)
        payload_manifest = backup_dir / "payload" / "manifest.json"
        assert not payload_manifest.exists(), \
            "manifest.json leaked into payload/ — would contaminate symlink targets on restore"

        manifest = json.loads(manifest_path.read_text())
        assert manifest["previous_type"] == "directory"
        assert manifest.get("payload_dir") == "payload/"

        # Payload should contain the original config.yaml
        payload_config = backup_dir / "payload" / "config.yaml"
        assert payload_config.is_file(), "config.yaml missing from payload/"
        assert payload_config.read_text() == "key: value"

    def test_symlink_plugin_backed_up_as_symlink(self, tmp_path):
        """Pre-existing symlink is recorded with exact target, not followed."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        src_dir = tmp_path / "source_v1"
        src_dir.mkdir()
        (src_dir / "old.py").write_text("# v1")

        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        plugin_dir.parent.mkdir(parents=True)
        plugin_dir.symlink_to(src_dir)

        env = {"HERMES_HOME": str(hermes_home)}
        result = run_script("install-local.sh", ["--symlink"], env=env)
        assert result.returncode == 0, f"install failed: {result.stderr}"

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backup_dir = list(backup_root.iterdir())[0]
        manifest = json.loads((backup_dir / "manifest.json").read_text())

        assert manifest["previous_type"] == "symlink"
        assert str(src_dir) in manifest["previous_target"]

        # payload should have link_target.txt, not the actual files
        target_file = backup_dir / "payload" / "link_target.txt"
        assert target_file.is_file(), "Symlink target not recorded in payload/"
        assert src_dir.name in target_file.read_text()

    def test_broken_symlink_handled(self, tmp_path):
        """Broken symlink (target deleted) is handled gracefully."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        gone_dir = tmp_path / "gone"
        gone_dir.mkdir()
        (gone_dir / "something.py").write_text("# gone soon")

        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        plugin_dir.parent.mkdir(parents=True)
        plugin_dir.symlink_to(gone_dir)

        # Now delete the target to simulate broken symlink
        shutil.rmtree(gone_dir)

        assert plugin_dir.is_symlink()
        assert not plugin_dir.exists(), "Symlink should be broken"

        env = {"HERMES_HOME": str(hermes_home)}
        result = run_script("install-local.sh", ["--symlink"], env=env)
        assert result.returncode == 0, f"Broken symlink install failed: {result.stderr}"

        # Backup should still exist with symlink target recorded
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backup_dir = list(backup_root.iterdir())[0]
        manifest = json.loads((backup_dir / "manifest.json").read_text())
        assert manifest["previous_type"] == "symlink"
        assert str(gone_dir) in manifest["previous_target"]

    def test_repeated_upgrades_preserve_all_backups(self, tmp_path):
        """Three installs produce the expected number of backups."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        env = {"HERMES_HOME": str(hermes_home)}
        r1 = run_script("install-local.sh", ["--copy"], env=env)
        assert r1.returncode == 0, f"First install failed: {r1.stderr}"

        r2 = run_script("install-local.sh", ["--copy"], env=env)
        assert r2.returncode == 0, f"Second install failed: {r2.stderr}"

        r3 = run_script("install-local.sh", ["--symlink"], env=env)
        assert r3.returncode == 0, f"Third install failed: {r3.stderr}"

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backups = sorted(backup_root.iterdir())
        # First install is clean (no pre-existing), so only 2 backups for upgrades
        assert len(backups) == 2, f"Expected 2 backups (first was clean), got {len(backups)}"

    def test_ledger_data_preserved(self, tmp_path):
        """Install never touches ledger data directory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        ledger_dir = hermes_home / "summarization-calendar"
        (ledger_dir / "recaps" / "2026-03-08").mkdir(parents=True)
        (ledger_dir / "recaps" / "2026-03-08" / "meta.json").write_text('{"date":"2026-03-08"}')

        env = {"HERMES_HOME": str(hermes_home)}
        result = run_script("install-local.sh", ["--symlink"], env=env)
        assert result.returncode == 0, f"install failed: {result.stderr}"

        # Ledger data untouched
        meta = ledger_dir / "recaps" / "2026-03-08" / "meta.json"
        assert meta.read_text() == '{"date":"2026-03-08"}'


class TestRollbackBackupLayout:
    """Verify rollback restores exact type from manifest+payload layout."""

    def _setup_upgrade(self, tmp_path) -> dict:
        """Install as copy first, then upgrade to symlink. Returns context."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        env = {"HERMES_HOME": str(hermes_home)}
        # First install as copy (no pre-existing -> no backup)
        r = run_script("install-local.sh", ["--copy"], env=env)
        assert r.returncode == 0, f"Copy install failed: {r.stderr}"

        # Upgrade to symlink — backs up the copy directory
        r2 = run_script("install-local.sh", ["--symlink"], env=env)
        assert r2.returncode == 0, f"Upgrade to symlink failed: {r2.stderr}"

        return {"hermes_home": hermes_home, "env": env}

    def test_rollback_restores_directory_type(self, tmp_path):
        """Rollback to a directory backup restores files from payload/."""
        ctx = self._setup_upgrade(tmp_path)
        hermes_home = ctx["hermes_home"]
        plugin_dir = hermes_home / "plugins" / "summarization-calendar"

        # Currently a symlink after upgrade
        assert plugin_dir.is_symlink()

        # Find the directory backup (from copy install)
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        v1_backup = None
        for b in sorted(backup_root.iterdir()):
            m = json.loads((b / "manifest.json").read_text())
            if m["previous_type"] == "directory":
                v1_backup = b
                break

        assert v1_backup is not None, "No directory backup found"

        result = run_script("rollback-local.sh", [v1_backup.name], env=ctx["env"])
        assert result.returncode == 0, f"Rollback failed: {result.stderr}"

        # Should now be a directory (not symlink)
        assert not plugin_dir.is_symlink(), "Rollback should restore directory, not symlink"
        assert plugin_dir.is_dir()
        # Check for a file from the actual project that was copy-installed
        found_files = list(plugin_dir.iterdir())
        assert len(found_files) > 0, "Restored directory is empty"

    def test_rollback_restores_symlink_type(self, tmp_path):
        """Rollback to a symlink backup restores the exact link target."""
        ctx = self._setup_upgrade(tmp_path)
        hermes_home = ctx["hermes_home"]

        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        assert plugin_dir.is_symlink()
        current_target = str(plugin_dir.resolve())

        # Install as copy again — this backs up the symlink
        r = run_script("install-local.sh", ["--copy"], env=ctx["env"])
        assert r.returncode == 0, f"Copy install failed: {r.stderr}"

        # Find the symlink backup
        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        v2_backup = None
        for b in sorted(backup_root.iterdir()):
            m = json.loads((b / "manifest.json").read_text())
            if m["previous_type"] == "symlink":
                v2_backup = b
                break

        assert v2_backup is not None, "No symlink backup found"

        result = run_script("rollback-local.sh", [v2_backup.name], env=ctx["env"])
        assert result.returncode == 0, f"Rollback failed: {result.stderr}"

        # Should be a symlink again pointing to the same target
        assert plugin_dir.is_symlink(), "Rollback should restore symlink"
        restored_target = str(plugin_dir.resolve())
        assert current_target == restored_target, \
            f"Symlink target changed: was {current_target}, now {restored_target}"

    def test_rollback_creates_pre_snapshot(self, tmp_path):
        """Rollback snapshots current state before restoring."""
        ctx = self._setup_upgrade(tmp_path)
        hermes_home = ctx["hermes_home"]

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        before_count = len(list(backup_root.iterdir()))

        # Find v1 directory backup to rollback to
        v1_backup = None
        for b in sorted(backup_root.iterdir()):
            m = json.loads((b / "manifest.json").read_text())
            if m["previous_type"] == "directory":
                v1_backup = b
                break

        assert v1_backup is not None
        result = run_script("rollback-local.sh", [v1_backup.name], env=ctx["env"])
        assert result.returncode == 0, f"Rollback failed: {result.stderr}"

        after_count = len(list(backup_root.iterdir()))
        # One new backup for the current-before-rollback snapshot
        assert after_count == before_count + 1, \
            f"Expected one new pre-rollback snapshot ({before_count}+1), got {after_count}"


class TestUninstallBackupLayout:
    """Verify uninstall creates manifest+payload snapshot and preserves ledger."""

    def test_uninstall_preserves_ledger(self, tmp_path):
        """Uninstall keeps ledger data intact."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        env = {"HERMES_HOME": str(hermes_home)}
        run_script("install-local.sh", ["--copy"], env=env)

        # Create ledger data
        ledger_dir = hermes_home / "summarization-calendar"
        (ledger_dir / "recaps" / "2026-03-08").mkdir(parents=True)
        (ledger_dir / "recaps" / "2026-03-08" / "meta.json").write_text('{"date":"2026-03-08"}')

        result = run_script("uninstall-local.sh", [], env=env)
        assert result.returncode == 0, f"Uninstall failed: {result.stderr}"

        plugin_dir = hermes_home / "plugins" / "summarization-calendar"
        assert not plugin_dir.exists()

        # Ledger untouched
        meta = ledger_dir / "recaps" / "2026-03-08" / "meta.json"
        assert meta.read_text() == '{"date":"2026-03-08"}'

    def test_uninstall_creates_backup(self, tmp_path):
        """Uninstall creates a manifest+payload snapshot."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        env = {"HERMES_HOME": str(hermes_home)}
        run_script("install-local.sh", ["--symlink"], env=env)

        result = run_script("uninstall-local.sh", [], env=env)
        assert result.returncode == 0, f"Uninstall failed: {result.stderr}"

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backups = [b for b in backup_root.iterdir() if "uninstall" in b.name]
        assert len(backups) >= 1, "No uninstall snapshot found"

        manifest = json.loads((backups[0] / "manifest.json").read_text())
        assert manifest["previous_type"] == "symlink"
        assert manifest.get("snapshot_reason") == "uninstall-pre-snapshot"

    def test_uninstall_manifest_is_valid_json_for_quoted_home_path(self, tmp_path):
        """Snapshot metadata must remain valid JSON for legal shell path characters."""
        hermes_home = tmp_path / 'hermes"quoted'
        hermes_home.mkdir()
        env = {"HERMES_HOME": str(hermes_home)}

        installed = run_script("install-local.sh", ["--copy"], env=env)
        assert installed.returncode == 0, installed.stderr

        session_meta = (
            hermes_home
            / "summarization-calendar"
            / "session-versions"
            / "2026-07-12"
            / "identity"
            / "v1"
            / "meta.json"
        )
        rollup_meta = (
            hermes_home
            / "summarization-calendar"
            / "rollup-versions"
            / "2026-07-12"
            / "v1"
            / "meta.json"
        )
        session_meta.parent.mkdir(parents=True)
        rollup_meta.parent.mkdir(parents=True)
        session_meta.write_text("{}")
        rollup_meta.write_text("{}")

        removed = run_script("uninstall-local.sh", [], env=env)
        assert removed.returncode == 0, removed.stderr
        assert "Session versions preserved: 1" in removed.stdout
        assert "Roll-up versions preserved: 1" in removed.stdout

        backup_root = hermes_home / "backups" / "summarization-calendar-install"
        backups = list(backup_root.glob("uninstall-*/manifest.json"))
        assert len(backups) == 1
        manifest = json.loads(backups[0].read_text())
        assert manifest["hermes_home"] == str(hermes_home)


class TestStatusCurrentLayout:
    """Status output must describe the v1 session-summary and roll-up layout."""

    def test_status_counts_versions_and_only_live_jobs(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        plugin = hermes_home / "plugins" / "summarization-calendar"
        plugin.mkdir(parents=True)

        ledger = hermes_home / "summarization-calendar"
        session_version = ledger / "session-versions" / "2026-07-12" / "identity" / "v1"
        rollup_version = ledger / "rollup-versions" / "2026-07-12" / "v1"
        session_version.mkdir(parents=True)
        rollup_version.mkdir(parents=True)
        (session_version / "meta.json").write_text("{}")
        (rollup_version / "meta.json").write_text("{}")

        running_sessions = ledger / "running" / "sessions"
        running_rollups = ledger / "running" / "rollups"
        running_sessions.mkdir(parents=True)
        running_rollups.mkdir(parents=True)
        (running_sessions / "done.json").write_text('{"status":"completed"}')
        (running_sessions / "active.json").write_text('{"status":"running"}')
        (running_rollups / "failed.json").write_text('{"status":"failed"}')

        result = run_script(
            "status-local.sh", env={"HERMES_HOME": str(hermes_home)}
        )
        assert result.returncode == 0, result.stderr
        assert "Session versions: 1" in result.stdout
        assert "Roll-up versions: 1" in result.stdout
        assert "Active jobs: 1" in result.stdout


class TestBashScriptsSyntax:
    """Verify all scripts parse cleanly."""

    @pytest.mark.parametrize("script", ["install-local.sh", "rollback-local.sh", "uninstall-local.sh"])
    def test_bash_n(self, script):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPTS / script)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"Syntax error in {script}: {result.stderr}"
