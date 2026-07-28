"""Failure-atomic storage tests for per-session summaries and roll-ups."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from hermes_daily_ledger.session_storage import (
    SummaryVersion,
    artifact_key,
    check_session_staleness,
    list_rollup_versions,
    list_session_versions,
    load_rollup,
    load_session_summary,
    rollback_session_summary,
    rollback_rollup,
    save_rollup,
    save_session_summary,
    session_summary_exists,
)


DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"


@pytest.fixture
def ledger_root(tmp_path: Path) -> Path:
    root = tmp_path / "ledger"
    root.mkdir(mode=0o700)
    return root


def _data(summary: str = "A useful summary") -> dict:
    return {
        "summary": summary,
        "key_points": ["one", "two"],
    }


def test_artifact_key_is_deterministic_and_path_safe() -> None:
    key = artifact_key(DATE, "../escape-profile", "../../etc/passwd")
    assert key == artifact_key(DATE, "../escape-profile", "../../etc/passwd")
    assert re.fullmatch(r"[0-9a-f]{32}", key)
    assert "escape-profile" not in key
    assert "/" not in key
    assert ".." not in key


def test_invalid_date_is_rejected_before_filesystem_access(ledger_root: Path) -> None:
    with pytest.raises(ValueError):
        save_session_summary(
            "../../etc", PROFILE, SESSION_ID, "Title", _data(), "sha256:fp",
            ledger_root=ledger_root,
        )
    assert list(ledger_root.iterdir()) == []


def test_save_and_load_session_summary(ledger_root: Path) -> None:
    version = save_session_summary(
        DATE,
        PROFILE,
        SESSION_ID,
        "DST migration task",
        _data(),
        "sha256:abc",
        generated_at="2026-03-08T12:00:00Z",
        collection_cutoff_utc="2026-03-09T05:00:00Z",
        model_profile="auxiliary.compression",
        model="",
        ledger_root=ledger_root,
    )

    assert isinstance(version, SummaryVersion)
    assert version.profile == PROFILE
    assert version.session_id == SESSION_ID
    raw, meta = load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root)
    assert raw == _data()
    assert meta is not None
    assert meta["title"] == "DST migration task"
    assert meta["source_fingerprint"] == "sha256:abc"
    assert meta["artifact_key"] == artifact_key(DATE, PROFILE, SESSION_ID)
    assert "messages" not in json.dumps(meta).lower()
    assert session_summary_exists(DATE, PROFILE, SESSION_ID, ledger_root)

    current = ledger_root / "session-summaries" / DATE / meta["artifact_key"]
    assert current.is_symlink()
    assert current.resolve().parent.parent.parent == ledger_root / "session-versions"


def test_second_save_preserves_immutable_first_version(ledger_root: Path) -> None:
    first = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("v1"), "sha256:v1",
        ledger_root=ledger_root,
    )
    second = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("v2"), "sha256:v2",
        ledger_root=ledger_root,
    )

    assert first.version_id != second.version_id
    versions = list_session_versions(DATE, PROFILE, SESSION_ID, ledger_root)
    assert {v.version_id for v in versions} == {first.version_id, second.version_id}
    raw, _ = load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root)
    assert raw == _data("v2")
    first_dir = (
        ledger_root / "session-versions" / DATE /
        artifact_key(DATE, PROFILE, SESSION_ID) / first.version_id
    )
    assert json.loads((first_dir / "raw.json").read_text()) == _data("v1")


def test_pointer_swap_failure_preserves_current(ledger_root: Path) -> None:
    first = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("original"), "sha256:old",
        ledger_root=ledger_root,
    )

    with patch(
        "hermes_daily_ledger.session_storage._install_strict_current_pointer",
        side_effect=OSError("simulated pointer failure"),
    ):
        with pytest.raises(OSError, match="pointer failure"):
            save_session_summary(
                DATE, PROFILE, SESSION_ID, "Title", _data("replacement"),
                "sha256:new", ledger_root=ledger_root,
            )

    raw, meta = load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root)
    assert raw == _data("original")
    assert meta is not None and meta["version_id"] == first.version_id


def test_version_collision_fails_closed_without_overwrite(ledger_root: Path) -> None:
    fixed = "20260308T120000Z_123456_abcdef123456"
    with patch("hermes_daily_ledger.session_storage._new_version_id", return_value=fixed):
        save_session_summary(
            DATE, PROFILE, SESSION_ID, "Title", _data("v1"), "sha256:v1",
            ledger_root=ledger_root,
        )
        with pytest.raises(FileExistsError):
            save_session_summary(
                DATE, PROFILE, SESSION_ID, "Title", _data("v2"), "sha256:v2",
                ledger_root=ledger_root,
            )

    raw, _ = load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root)
    assert raw == _data("v1")


def test_rollback_repoints_to_complete_matching_version(ledger_root: Path) -> None:
    first = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("v1"), "sha256:v1",
        ledger_root=ledger_root,
    )
    save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("v2"), "sha256:v2",
        ledger_root=ledger_root,
    )

    restored = rollback_session_summary(
        DATE, PROFILE, SESSION_ID, first.version_id, ledger_root
    )
    assert restored is not None and restored.version_id == first.version_id
    raw, _ = load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root)
    assert raw == _data("v1")

    assert rollback_session_summary(
        DATE, PROFILE, SESSION_ID, "../../escape", ledger_root
    ) is None


def test_staleness_is_scoped_to_session_fingerprint(ledger_root: Path) -> None:
    save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data(), "sha256:current",
        ledger_root=ledger_root,
    )
    assert not check_session_staleness(
        DATE, PROFILE, SESSION_ID, "sha256:current", ledger_root
    )
    assert check_session_staleness(
        DATE, PROFILE, SESSION_ID, "sha256:changed", ledger_root
    )


def test_raw_transcript_shapes_are_rejected(ledger_root: Path) -> None:
    for payload in (
        {"summary": "ok", "messages": [{"content": "raw"}]},
        {"summary": "ok", "transcript": "raw"},
        {"summary": "ok", "nested": {"system_prompt": "secret"}},
    ):
        with pytest.raises(ValueError, match="raw transcript"):
            save_session_summary(
                DATE, PROFILE, SESSION_ID, "Title", payload, "sha256:fp",
                ledger_root=ledger_root,
            )


def test_restrictive_permissions(ledger_root: Path) -> None:
    version = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data(), "sha256:fp",
        ledger_root=ledger_root,
    )
    key = artifact_key(DATE, PROFILE, SESSION_ID)
    version_dir = ledger_root / "session-versions" / DATE / key / version.version_id
    assert stat.S_IMODE(version_dir.stat().st_mode) == 0o700
    for filename in ("meta.json", "raw.json", "summary.md"):
        assert stat.S_IMODE((version_dir / filename).stat().st_mode) == 0o600


def test_save_rejects_symlinked_version_parent(ledger_root: Path, tmp_path: Path) -> None:
    key = artifact_key(DATE, PROFILE, SESSION_ID)
    external = tmp_path / "external"
    external.mkdir()
    date_dir = ledger_root / "session-versions" / DATE
    date_dir.mkdir(parents=True)
    (date_dir / key).symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="Unsafe directory component"):
        save_session_summary(
            DATE, PROFILE, SESSION_ID, "Title", _data(), "sha256:fp",
            ledger_root=ledger_root,
        )
    assert list(external.iterdir()) == []


def test_save_rejects_symlinked_lock_file(ledger_root: Path, tmp_path: Path) -> None:
    lock_dir = ledger_root / ".locks"
    lock_dir.mkdir(parents=True)
    key = artifact_key(DATE, PROFILE, SESSION_ID)
    external = tmp_path / "outside.lock"
    external.write_text("sentinel", encoding="utf-8")
    (lock_dir / f"session-summary-{key}.lock").symlink_to(external)

    with pytest.raises(OSError):
        save_session_summary(
            DATE, PROFILE, SESSION_ID, "Title", _data(), "sha256:fp",
            ledger_root=ledger_root,
        )
    assert external.read_text(encoding="utf-8") == "sentinel"


def test_real_directory_at_current_pointer_is_preserved_and_rejected(
    ledger_root: Path,
) -> None:
    first = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data("first"), "sha256:first",
        ledger_root=ledger_root,
    )
    key = artifact_key(DATE, PROFILE, SESSION_ID)
    current = ledger_root / "session-summaries" / DATE / key
    current.unlink()
    current.mkdir()
    sentinel = current / "do-not-delete.txt"
    sentinel.write_text("preserve me", encoding="utf-8")

    with pytest.raises(OSError):
        save_session_summary(
            DATE, PROFILE, SESSION_ID, "Title", _data("second"), "sha256:second",
            ledger_root=ledger_root,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve me"
    assert (
        ledger_root / "session-versions" / DATE / key / first.version_id
    ).is_dir()


def test_load_fails_closed_on_canonical_identity_mismatch(ledger_root: Path) -> None:
    version = save_session_summary(
        DATE, PROFILE, SESSION_ID, "Title", _data(), "sha256:fp",
        ledger_root=ledger_root,
    )
    key = artifact_key(DATE, PROFILE, SESSION_ID)
    meta_path = (
        ledger_root / "session-versions" / DATE / key / version.version_id / "meta.json"
    )
    meta = json.loads(meta_path.read_text())
    meta["session_id"] = "different-session"
    meta_path.write_text(json.dumps(meta))

    assert load_session_summary(DATE, PROFILE, SESSION_ID, ledger_root) == (None, None)


def test_rollup_storage_is_separate_and_records_coverage(ledger_root: Path) -> None:
    data = {
        "overall_recap": "A compact day.",
        "included_sessions": [{"profile": PROFILE, "session_id": SESSION_ID}],
        "coverage": {"included": 1, "active": 2},
    }
    version = save_rollup(
        DATE,
        data,
        "sha256:rollup-input",
        generated_at="2026-03-08T13:00:00Z",
        ledger_root=ledger_root,
    )
    raw, meta = load_rollup(DATE, ledger_root)
    assert raw == data
    assert meta is not None
    assert meta["artifact_kind"] == "rollup"
    assert meta["version_id"] == version.version_id
    assert not (ledger_root / "recaps" / DATE).exists()


def test_rollup_versions_and_rollback(ledger_root: Path) -> None:
    first_data = {
        "overall_recap": "first",
        "included_sessions": [],
        "coverage": {"included": 0, "active": 1},
    }
    second_data = {**first_data, "overall_recap": "second"}
    first = save_rollup(DATE, first_data, "sha256:first", ledger_root=ledger_root)
    second = save_rollup(DATE, second_data, "sha256:second", ledger_root=ledger_root)

    versions = list_rollup_versions(DATE, ledger_root)
    assert {item.version_id for item in versions} == {
        first.version_id,
        second.version_id,
    }
    restored = rollback_rollup(DATE, first.version_id, ledger_root)
    assert restored is not None and restored.version_id == first.version_id
    raw, _ = load_rollup(DATE, ledger_root)
    assert raw == first_data
    assert rollback_rollup(DATE, "../../escape", ledger_root) is None
