"""Regression tests for the detached batch worker wrapper."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import plugin_api


def test_failed_batch_log_does_not_report_unknown(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    """A normal all-failed batch has useful diagnostics, not a fake unknown error."""
    removed: list[str] = []

    monkeypatch.setattr(
        plugin_api,
        "run_batch_summary",
        lambda *args, **kwargs: {
            "status": "failed",
            "failed": 2,
            "error": None,
        },
    )
    monkeypatch.setattr(plugin_api, "_remove_worker", removed.append)

    with caplog.at_level(logging.ERROR, logger=plugin_api.logger.name):
        plugin_api._run_batch_worker(
            "2026-03-08",
            "batch-001",
            "2026-03-08:batch-001",
            tmp_path / "ledger",
        )

    assert removed == ["2026-03-08:batch-001"]
    assert "unknown" not in caplog.text.lower()
    assert "2 member(s) failed" in caplog.text
