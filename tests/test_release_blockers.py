"""Release-blocker tests found by independent failure injection after 11357eb."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import plugin_api as api
from hermes_summarization_calendar import recap_storage as rs
from hermes_summarization_calendar.chunker import _split_content_utf8, chunk_transcripts
from hermes_summarization_calendar.transcript import TranscriptMessage

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install-local.sh"
ROLLBACK = REPO / "scripts" / "rollback-local.sh"

DATA1 = {
    "session_summaries": [
        {"profile": "default", "session_id": "s", "title": "t", "summary": "one"}
    ],
    "overall_recap": "one",
}
DATA2 = {
    "session_summaries": [
        {"profile": "default", "session_id": "s", "title": "t", "summary": "two"}
    ],
    "overall_recap": "two",
}


def test_save_pointer_replace_failure_preserves_current(tmp_path):
    rs.save_recap("2026-07-20", DATA1, "fp1", ledger_root=tmp_path)
    before = rs.load_recap("2026-07-20", ledger_root=tmp_path)

    with patch.object(rs.os, "replace", side_effect=OSError("injected pointer failure")):
        with pytest.raises(OSError):
            rs.save_recap("2026-07-20", DATA2, "fp2", ledger_root=tmp_path)

    assert rs.load_recap("2026-07-20", ledger_root=tmp_path) == before


def test_rollback_pointer_replace_failure_preserves_current(tmp_path):
    first = rs.save_recap("2026-07-20", DATA1, "fp1", ledger_root=tmp_path)
    rs.save_recap("2026-07-20", DATA2, "fp2", ledger_root=tmp_path)
    before = rs.load_recap("2026-07-20", ledger_root=tmp_path)

    with patch.object(rs.os, "replace", side_effect=OSError("injected pointer failure")):
        with pytest.raises(OSError):
            rs.rollback_to_version("2026-07-20", first.version_ts, ledger_root=tmp_path)

    assert rs.load_recap("2026-07-20", ledger_root=tmp_path) == before


def test_version_collision_cannot_mutate_immutable_version(tmp_path):
    fixed = "20260720T120000Z_000001_deadbeefcafe"
    with patch.object(rs, "_version_timestamp", return_value=fixed):
        rs.save_recap("2026-07-20", DATA1, "fp1", ledger_root=tmp_path)
        version_file = tmp_path / "versions" / "2026-07-20" / fixed / "raw.json"
        before = version_file.read_bytes()
        with pytest.raises(FileExistsError):
            rs.save_recap("2026-07-20", DATA2, "fp2", ledger_root=tmp_path)
        assert version_file.read_bytes() == before


def test_invalid_install_mode_preserves_existing_plugin(tmp_path):
    home = tmp_path / ".hermes"
    plugin = home / "plugins" / "summarization-calendar"
    plugin.mkdir(parents=True)
    (plugin / "keep.txt").write_text("keep")

    result = subprocess.run(
        ["bash", str(INSTALL), "--invalid-mode"],
        env={**os.environ, "HERMES_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert (plugin / "keep.txt").read_text() == "keep"


def test_legacy_backup_with_only_subdirectories_restores(tmp_path):
    home = tmp_path / ".hermes"
    backup = home / "backups" / "summarization-calendar-install" / "legacy-dir-only"
    (backup / "dashboard").mkdir(parents=True)
    (backup / "dashboard" / "index.js").write_text("ok")
    (backup / "manifest.json").write_text(
        json.dumps({"backup_id": "legacy-dir-only", "previous_type": "directory"})
    )

    result = subprocess.run(
        ["bash", str(ROLLBACK), "legacy-dir-only"],
        env={**os.environ, "HERMES_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert (home / "plugins" / "summarization-calendar" / "dashboard" / "index.js").read_text() == "ok"


def test_empty_new_layout_directory_restores_exact_type(tmp_path):
    home = tmp_path / ".hermes"
    backup = home / "backups" / "summarization-calendar-install" / "empty-dir"
    (backup / "payload").mkdir(parents=True)
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "backup_id": "empty-dir",
                "previous_type": "directory",
                "payload_dir": "payload/",
            }
        )
    )

    result = subprocess.run(
        ["bash", str(ROLLBACK), "empty-dir"],
        env={**os.environ, "HERMES_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )

    plugin = home / "plugins" / "summarization-calendar"
    assert result.returncode == 0, result.stderr
    assert plugin.is_dir() and not plugin.is_symlink()
    assert list(plugin.iterdir()) == []


def test_utf8_split_never_exceeds_requested_bytes():
    content = "é世क🙂" * 20
    parts = _split_content_utf8(content, 7)
    assert "".join(parts) == content
    assert all(len(part.encode("utf-8")) <= 7 for part in parts)


def test_many_oversized_segments_are_lossless_without_iteration_cap():
    messages = [
        TranscriptMessage(role="user", content=f"{i:03d}:" + "x" * 700, tool_name=None)
        for i in range(140)
    ]

    class LargeTranscript:
        session_id = "many-segments"
        profile = "default"
        title = "Many segments"
        source = "cli"
        model = "model"

    transcript = LargeTranscript()
    transcript.messages = messages
    chunks = chunk_transcripts([transcript], safe_ceiling=3_500)
    rebuilt = []
    for chunk in chunks:
        for session in chunk.session_transcripts:
            rebuilt.extend(message["content"] for message in session["messages"])
    assert rebuilt == [message.content for message in messages]
    assert all(len(chunk.prompt_text.encode("utf-8")) <= 3_500 for chunk in chunks)


def test_pending_workers_count_toward_global_limit(monkeypatch, tmp_path):
    import plugin_api as api
    import hermes_summarization_calendar.concurrency as concurrency

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
    monkeypatch.setattr(api, "recap_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(api, "acquire_generation_slot", lambda *args, **kwargs: True)
    monkeypatch.setattr(concurrency, "release_generation_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.threading, "Thread", PendingThread)

    try:
        for day in range(1, 5):
            api.post_recap(f"2026-07-{day:02d}", api.RecapRequestBody(force_regenerate=True))
        with pytest.raises(HTTPException) as exc:
            api.post_recap("2026-07-05", api.RecapRequestBody(force_regenerate=True))
        assert exc.value.status_code == 503
    finally:
        api._worker_pool.clear()


def test_startup_recovery_failure_is_retried(monkeypatch):
    import plugin_api as api

    calls = []
    def recover():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")
        return []

    monkeypatch.setattr(api, "recover_stale_locks", recover)
    api._startup_done = False
    try:
        try:
            api._on_startup()
        except OSError:
            pass
        api._on_startup()
        assert len(calls) == 2
        assert api._startup_done is True
    finally:
        api._startup_done = False


def _write_failing_mv_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    """Return a PATH directory whose mv fails on configured invocation numbers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    count_file = tmp_path / "mv-count"
    wrapper = bin_dir / "mv"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "n=0\n"
        "[ ! -f \"$MV_COUNT_FILE\" ] || n=$(< \"$MV_COUNT_FILE\")\n"
        "n=$((n + 1))\n"
        "printf '%s' \"$n\" > \"$MV_COUNT_FILE\"\n"
        "case \",$MV_FAIL_CALLS,\" in *,$n,*) exit 97 ;; esac\n"
        "exec /usr/bin/mv \"$@\"\n"
    )
    wrapper.chmod(0o755)
    return bin_dir, count_file


def test_legacy_directory_pointer_failure_restores_directory(tmp_path):
    current = tmp_path / "recaps" / "2026-07-20"
    current.mkdir(parents=True)
    (current / "keep.txt").write_text("legacy")

    with patch.object(rs.os, "replace", side_effect=OSError("injected pointer failure")):
        with pytest.raises(OSError):
            rs.save_recap("2026-07-20", DATA1, "fp1", ledger_root=tmp_path)

    assert current.is_dir() and not current.is_symlink()
    assert (current / "keep.txt").read_text() == "legacy"


def test_legacy_cleanup_failure_does_not_misreport_successful_swap(tmp_path):
    current = tmp_path / "recaps" / "2026-07-20"
    current.mkdir(parents=True)
    (current / "keep.txt").write_text("legacy")
    original_rmtree = rs.shutil.rmtree

    def fail_legacy_cleanup(path, *args, **kwargs):
        if ".legacy-" in str(path):
            raise OSError("injected cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    with patch.object(rs.shutil, "rmtree", side_effect=fail_legacy_cleanup):
        rs.save_recap("2026-07-20", DATA1, "fp1", ledger_root=tmp_path)

    loaded, _ = rs.load_recap("2026-07-20", ledger_root=tmp_path)
    assert loaded == DATA1


def test_payload_directory_symlink_is_rejected_without_touching_current(tmp_path):
    home = tmp_path / ".hermes"
    plugin = home / "plugins" / "summarization-calendar"
    plugin.mkdir(parents=True)
    (plugin / "keep.txt").write_text("keep")
    external = tmp_path / "external"
    external.mkdir()
    (external / "foreign.txt").write_text("foreign")
    backup = home / "backups" / "summarization-calendar-install" / "symlink-payload"
    backup.mkdir(parents=True)
    (backup / "payload").symlink_to(external, target_is_directory=True)
    (backup / "manifest.json").write_text(
        json.dumps({"backup_id": "symlink-payload", "previous_type": "directory"})
    )

    result = subprocess.run(
        ["bash", str(ROLLBACK), "symlink-payload"],
        env={**os.environ, "HERMES_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert (plugin / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize(
    ("script", "args", "old_glob"),
    [
        (INSTALL, ["--copy"], ".summarization-calendar-install-old-*"),
        (ROLLBACK, ["restore-dir"], ".summarization-calendar-restore-old-*"),
    ],
)
def test_double_swap_failure_preserves_recoverable_previous_plugin(
    tmp_path, script, args, old_glob
):
    home = tmp_path / ".hermes"
    plugin_parent = home / "plugins"
    plugin = plugin_parent / "summarization-calendar"
    plugin.mkdir(parents=True)
    (plugin / "keep.txt").write_text("keep")
    if script == ROLLBACK:
        backup = home / "backups" / "summarization-calendar-install" / "restore-dir"
        (backup / "payload").mkdir(parents=True)
        (backup / "payload" / "restored.txt").write_text("restored")
        (backup / "manifest.json").write_text(
            json.dumps({"backup_id": "restore-dir", "previous_type": "directory"})
        )

    bin_dir, count_file = _write_failing_mv_wrapper(tmp_path)
    result = subprocess.run(
        ["bash", str(script), *args],
        env={
            **os.environ,
            "HERMES_HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MV_COUNT_FILE": str(count_file),
            "MV_FAIL_CALLS": "2,3",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    survivors = []
    if (plugin / "keep.txt").is_file():
        survivors.append(plugin)
    survivors.extend(
        path for path in plugin_parent.glob(old_glob) if (path / "keep.txt").is_file()
    )
    assert survivors, "swap and restore failure deleted the only prior plugin copy"


def test_json_escaping_content_is_split_by_actual_prompt_size():
    message = TranscriptMessage(
        role="user",
        content=('"\\\n\t🙂' * 2_000),
        tool_name=None,
    )

    class EscapedTranscript:
        session_id = "escaped"
        profile = "default"
        title = "Escaped"
        source = "cli"
        model = "model"
        messages = [message]

    chunks = chunk_transcripts([EscapedTranscript()], safe_ceiling=3_500)
    rebuilt = "".join(
        msg["content"]
        for chunk in chunks
        for session in chunk.session_transcripts
        for msg in session["messages"]
    )
    assert rebuilt == message.content
    assert all(len(chunk.prompt_text.encode("utf-8")) <= 3_500 for chunk in chunks)


def test_get_recap_includes_public_version_history(monkeypatch, tmp_path: Path):
    """Calendar rollback controls need version metadata in the recap response."""
    from types import SimpleNamespace

    monkeypatch.setattr(api, "discover_all", lambda: ([], []))
    monkeypatch.setattr(
        api,
        "build_day_inventory",
        lambda *_args: SimpleNamespace(source_fingerprint="sha256:current"),
    )
    monkeypatch.setattr(api, "get_ledger_root", lambda: tmp_path)
    monkeypatch.setattr(
        api,
        "check_recap_status",
        lambda *_args: {
            "date": "2026-07-27",
            "exists": True,
            "meta": {"version_id": "v-current"},
            "data": {},
            "stale": False,
        },
    )
    monkeypatch.setattr(
        api,
        "_list_versions",
        lambda *_args: [
            SimpleNamespace(
                version_ts="20260727T010203Z_000001_deadbeefcafe",
                generated_at="2026-07-27T01:02:03+00:00",
                source_fingerprint="sha256:source",
                session_count=3,
                cron_count=2,
            )
        ],
    )

    result = api.get_recap("2026-07-27")
    assert result["versions"] == [
        {
            "version_id": "20260727T010203Z_000001_deadbeefcafe",
            "generated_at": "2026-07-27T01:02:03+00:00",
            "source_fingerprint": "sha256:source",
            "session_count": 3,
            "cron_count": 2,
        }
    ]
