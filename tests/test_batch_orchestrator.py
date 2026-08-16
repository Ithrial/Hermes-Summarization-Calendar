"""Tests for sequential batch summary coordinator (batch_orchestrator).

Uses tmp_path ledger roots and injected fakes.  Never launches auxiliary/model
inference or reads real Hermes stores.

Covers:
- strict stored order, one-at-a-time generation
- missing -> generate; stale -> regenerate; current -> skip; current+regen -> generate
- pre-existing running -> skipped_running
- acquire race (re-read running) -> skipped_running
- capacity failure -> failed but continues
- vanished session -> failed but continues
- immutable version_id capture on success
- returned failed job -> sanitized/generic error
- raised exception -> generic durable error + continuation + fail_session_job callback
- completed / all-skip / partial / all-failed derivation
- outer failure cleanup: no queued/running left
- exact slot_reserved=True call, no thread creation
- per-member inventory refresh with discover_all_deps
- no raw content/error leakage
- production-signature regression (discover + build per member)
- fail_session_job callback exactly once on raise, not on normal failed status
- inventory-build failure mid-batch
- narrowed _sanitize_error positive/negative tests
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import hermes_summarization_calendar.batch_jobs as batch_jobs
from hermes_summarization_calendar.summary_jobs import _reset_for_tests


# ---------------------------------------------------------------------------
# Fake dataclasses (mirror real shapes for type-checking)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeSession:
    session_id: str
    profile: str
    source: str = "fake"
    model: str = "fake"
    title: str = "Test Session"
    message_count: int = 1
    tool_call_count: int = 0
    first_active_utc: str | None = None
    last_active_utc: str | None = None
    source_fingerprint: str = "fp-1"


@dataclass(frozen=True)
class FakeDayInventory:
    date: str
    sessions: list[FakeSession] = field(default_factory=list)
    source_fingerprint: str = "fp-1"
    chicago_midnight_utc: str = ""
    chicago_next_midnight_utc: str = ""
    cron_runs: list = field(default_factory=list)


@dataclass(frozen=True)
class FakeJobStatus:
    kind: str = "session-summary"
    date: str = ""
    status: str = "running"
    profile: str = ""
    session_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    version_id: str | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_members(count: int) -> list[dict[str, str]]:
    return [{"profile": "p" + str(i), "session_id": "s" + str(i)} for i in range(count)]


def _create_batch(root: Path, date: str, bid: str, members: list[dict], **kw) -> dict:
    return batch_jobs.create_batch_job(root, date, bid, members, **kw)


def _fake_discover_all() -> tuple[list[Any], list[Any]]:
    """Return empty profile/cron lists for tests that don't need real discovery."""
    return [], []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBatchOrchestrator:

    def setup_method(self) -> None:
        _reset_for_tests()

    def test_terminal_batch_retry_is_idempotent_noop(self, tmp_path: Path) -> None:
        """A repeated coordinator call returns terminal state without rerunning work."""
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "terminal-retry"
        members = _make_members(1)
        _create_batch(root, date, bid, members)
        batch_jobs.start_batch_job(root, date, bid)
        batch_jobs.update_batch_member(
            root, date, bid, "p0", "s0", "completed", version_id="ver-0"
        )
        finalized = batch_jobs.finalize_batch_job(root, date, bid)

        result = run_batch_summary(date, bid, ledger_root=root)

        assert result == finalized
        assert result["status"] == "completed"
        assert result["finished_at"] == finalized["finished_at"]

    # -- strict order & single-generation-at-a-time -------------------------

    def test_strict_stored_order_and_one_at_a_time(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "order-test"
        members = _make_members(3)
        _create_batch(root, date, bid, members)

        call_order: list[str] = []
        active_flag = threading.Event()

        def build_inventory(date_str: str, profiles, cron_roots):
            return FakeDayInventory(
                date=date_str,
                sessions=[FakeSession(**m) for m in members],
            )

        def acquire_job(d, profile, sid, lr=None):
            return FakeJobStatus(date=d, profile=profile, session_id=sid)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            assert slot_reserved is True, "slot_reserved must be True"
            assert not active_flag.is_set(), "only one generation at a time"
            active_flag.set()
            try:
                call_order.append(profile + "/" + sid)
                return FakeJobStatus(
                    date=d, status="completed", profile=profile,
                    session_id=sid, version_id="ver-" + profile,
                )
            finally:
                active_flag.clear()

        result = run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=build_inventory,
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=acquire_job,
            generate_summary=generate_summary,
        )

        expected = ["p0/s0", "p1/s1", "p2/s2"]
        assert call_order == expected
        assert result["status"] == "completed"

    # -- missing -> generate ------------------------------------------------

    def test_missing_artifact_generates(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "missing-test"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        gen_called = False

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal gen_called
            gen_called = True
            assert slot_reserved is True
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-missing",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert gen_called

    # -- stale -> regenerate ------------------------------------------------

    def test_stale_artifact_regenerates(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "stale-test"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        gen_called = False

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal gen_called
            gen_called = True
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-stale",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: ({"summary": "old"}, {"source_fingerprint": "old-fp"}),
            check_staleness=lambda *a, **k: True,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert gen_called

    # -- current -> skip ----------------------------------------------------

    def test_current_artifact_skipped(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "current-skip"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        gen_called = False

        def generate_summary(**kw):
            nonlocal gen_called
            gen_called = True

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: ({"summary": "ok"}, {"source_fingerprint": "fp-1"}),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert not gen_called
        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "skipped_current"
        assert loaded["status"] == "completed"

    # -- explicit current regenerate ----------------------------------------

    def test_regenerate_current_overrides_skip(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "regen-current"
        members = _make_members(1)
        _create_batch(root, date, bid, members, regenerate_current=True)

        gen_called = False

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal gen_called
            gen_called = True
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-regen",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: ({"summary": "ok"}, {"source_fingerprint": "fp-1"}),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert gen_called

    # -- pre-existing running -> skipped_running ----------------------------

    def test_pre_existing_running_skipped(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "pre-running"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        def load_job(*a, **k):
            return FakeJobStatus(status="running")

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_job=load_job,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=lambda **kw: None,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "skipped_running"

    # -- acquire race -> skipped_running ------------------------------------

    def test_acquire_race_becomes_skipped_running(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "acquire-race"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        def load_job(*a, **k):
            return FakeJobStatus(status="running")

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_job=load_job,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: None,
            generate_summary=lambda **kw: None,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "skipped_running"

    # -- capacity failure -> failed but continues ---------------------------

    def test_capacity_failure_failed_continues(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "capacity-fail"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        call_count = 0

        def acquire_job(d, profile, sid, lr=None):
            if profile == "p0":
                return None
            return FakeJobStatus()

        def load_job(*a, **k):
            return None

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal call_count
            call_count += 1
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-cap",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_job=load_job,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=acquire_job,
            generate_summary=generate_summary,
        )

        assert call_count == 1
        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "failed"
        err0 = loaded["members"][0]["error"] or ""
        assert "capacity" in err0.lower()
        assert loaded["members"][1]["status"] == "completed"
        assert loaded["status"] == "partial"

    # -- vanished session -> failed but continues ---------------------------

    def test_vanished_session_failed_continues(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "vanished"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        inv_call = [0]

        def build_inventory(date_s: str, profiles, cron_roots):
            inv_call[0] += 1
            if inv_call[0] == 1:
                return FakeDayInventory(date=date_s, sessions=[])
            return FakeDayInventory(date=date_s, sessions=[FakeSession(**members[1])])

        gen_count = 0

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal gen_count
            gen_count += 1
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-vanish",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=build_inventory,
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert gen_count == 1
        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "failed"
        err0 = loaded["members"][0]["error"] or ""
        assert "no longer available" in err0.lower()
        assert loaded["members"][1]["status"] == "completed"

    # -- successful immutable version_id capture ----------------------------

    def test_version_id_captured_on_success(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "version-capture"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-" + profile + "-immutable",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["version_id"] == "ver-p0-immutable"
        assert loaded["members"][1]["version_id"] == "ver-p1-immutable"

    # -- returned failed job -> sanitized/generic error ---------------------

    def test_failed_job_sanitized_error(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "failed-sanitize"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            return FakeJobStatus(
                date=d, status="failed", profile=profile,
                session_id=sid, error="specific internal db error: /path/to/db",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "failed"
        error_text = loaded["members"][0]["error"] or ""
        assert "/path/to/db" not in error_text
        assert len(error_text) <= 500

    def test_failed_job_no_error_uses_generic(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "failed-generic"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            return FakeJobStatus(
                date=d, status="failed", profile=profile,
                session_id=sid, error=None,
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "failed"
        error_text = loaded["members"][0]["error"] or ""
        assert "Session summary generation failed" in error_text

    # -- raised exception -> generic durable error + continuation ----------

    def test_raised_exception_generic_error_continues(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "exception-cont"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            if profile == "p0":
                raise RuntimeError("internal error with /secret/path and API_KEY=xxx")
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-ok",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        err0 = loaded["members"][0]["error"] or ""
        assert "secret" not in err0.lower()
        assert "API_KEY" not in err0
        assert "/secret/path" not in err0
        assert loaded["members"][0]["status"] == "failed"
        assert loaded["members"][1]["status"] == "completed"

    # -- derivation: completed / all-skip / partial / all-failed -----------

    def test_all_completed_derivation(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "all-completed"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-" + profile,
            )

        result = run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )
        assert result["status"] == "completed"

    def test_all_skip_derivation(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "all-skip"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        result = run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: ({"summary": "ok"}, {"source_fingerprint": "fp-1"}),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=lambda **kw: None,
        )
        assert result["status"] == "completed"

    def test_partial_derivation(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "partial-deriv"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            if profile == "p0":
                return FakeJobStatus(
                    date=d, status="completed", profile=profile,
                    session_id=sid, version_id="ver-0",
                )
            return FakeJobStatus(
                date=d, status="failed", profile=profile,
                session_id=sid, error="gen failed",
            )

        result = run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )
        assert result["status"] == "partial"

    def test_all_failed_derivation(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "all-failed"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            return FakeJobStatus(
                date=d, status="failed", profile=profile,
                session_id=sid, error="always fails",
            )

        result = run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )
        assert result["status"] == "failed"

    # -- outer failure cleanup ---------------------------------------------

    def test_outer_failure_cleanup_no_queued_running(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "outer-fail"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        call_count = 0

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("coordinator-breaking error")
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-1",
            )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        for m in loaded["members"]:
            assert m["status"] not in ("queued", "running"), \
                "member " + str(m) + " left in non-terminal state"
        assert loaded["current"] is None

    # -- exact slot_reserved=True call & no thread creation ----------------

    def test_slot_reserved_true_and_no_threads(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "slot-reserved"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        slot_reserved_values: list[bool] = []
        thread_ids: list[int] = []

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            slot_reserved_values.append(slot_reserved)
            thread_ids.append(threading.get_ident())
            return FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-slot",
            )

        main_tid = threading.get_ident()
        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        assert slot_reserved_values == [True]
        assert all(tid == main_tid for tid in thread_ids), "no auxiliary threads created"

    # -- per-member inventory refresh --------------------------------------

    def test_inventory_refreshed_per_member(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "refresh-inv"
        members = _make_members(3)
        _create_batch(root, date, bid, members)

        inv_call_count = 0
        discover_call_count = 0

        def fake_discover():
            nonlocal discover_call_count
            discover_call_count += 1
            return [], []

        def build_inventory(date_s: str, profiles, cron_roots):
            nonlocal inv_call_count
            inv_call_count += 1
            return FakeDayInventory(date=date_s, sessions=[FakeSession(**m) for m in members])

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=build_inventory,
            discover_all_deps=fake_discover,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=lambda d, profile, sid, **kw: FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-" + profile,
            ),
        )

        assert inv_call_count == 3, "inventory built once per member"
        assert discover_call_count == 3, "discovery called once per member"

    # -- no raw content/error leakage --------------------------------------

    def test_no_raw_content_leakage(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "no-leak"
        members = _make_members(1)
        _create_batch(root, date, bid, members)

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            raise ValueError("Traceback: leaked /home/alice/.hermes/state.db content=SECRET")

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**members[0])]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)
        member = loaded["members"][0]
        err = (member["error"] or "").lower()
        assert "secret" not in err
        assert "/home/alice" not in err
        assert "traceback" not in err

    # =========================================================================
    # NEW TESTS for B1 fix
    # =========================================================================

    # -- FIX 1: production-signature regression (discover + build per member)

    def test_production_signature_discover_build_per_member(self, tmp_path: Path) -> None:
        """Prove that discover_all_deps and build_inventory are called per-member
        with REAL list arguments (not None), preserving per-member re-evaluation."""
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "prod-sig"
        members = _make_members(3)
        _create_batch(root, date, bid, members)

        discover_calls: list[tuple] = []
        build_calls: list[tuple] = []

        def fake_discover():
            profiles = ["prof-" + str(i) for i in range(len(members))]
            cron_roots = ["cron-root"]
            discover_calls.append((list(profiles), list(cron_roots)))
            return profiles, cron_roots

        def fake_build(date_s: str, profiles, cron_roots):
            # Verify called with real lists, not None
            assert profiles is not None, "profiles must not be None"
            assert cron_roots is not None, "cron_roots must not be None"
            assert isinstance(profiles, list), "profiles must be a list"
            assert isinstance(cron_roots, list), "cron_roots must be a list"
            build_calls.append((date_s, list(profiles), list(cron_roots)))
            return FakeDayInventory(date=date_s, sessions=[FakeSession(**m) for m in members])

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=fake_build,
            discover_all_deps=fake_discover,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=lambda d, profile, sid, **kw: FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-" + profile,
            ),
        )

        assert len(discover_calls) == 3, "discover called once per member"
        assert len(build_calls) == 3, "build called once per member"
        for dc in discover_calls:
            assert dc[0] is not None and isinstance(dc[0], list)
            assert dc[1] is not None and isinstance(dc[1], list)

    # -- FIX 2: fail_session_job callback on raise, not on normal failed

    def test_fail_session_job_called_on_raise_not_on_failed(self, tmp_path: Path) -> None:
        """After acquire succeeds, if generate_summary raises, fail_session_job_dep
        is called exactly once with correct identity + generic error.
        On a normal returned failed status, it is NOT called.
        Subsequent member still runs."""
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "fail-callback"
        members = _make_members(2)
        _create_batch(root, date, bid, members)

        fail_calls: list[tuple] = []

        def fake_fail(date_s, profile, sid, error, lr):
            fail_calls.append((date_s, profile, sid, error))

        gen_call_count = 0

        def generate_summary(d, profile, sid, *, slot_reserved=False, **kw):
            nonlocal gen_call_count
            gen_call_count += 1
            if profile == "p0":
                # Raises -> should trigger fail_session_job_dep
                raise RuntimeError("unexpected crash")
            elif profile == "p1":
                # Returns failed status -> should NOT trigger fail_session_job_dep
                return FakeJobStatus(
                    date=d, status="failed", profile=profile,
                    session_id=sid, error="returned failure",
                )

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=lambda d, p, c: FakeDayInventory(date=d, sessions=[FakeSession(**m) for m in members]),
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=generate_summary,
            fail_session_job_dep=fake_fail,
        )

        # fail_session_job_dep called exactly once: for the raised exception on p0
        assert len(fail_calls) == 1, f"expected 1 fail call, got {len(fail_calls)}: {fail_calls}"
        fc = fail_calls[0]
        assert fc[0] == date
        assert fc[1] == "p0"
        assert fc[2] == "s0"
        assert "Session summary generation failed" in fc[3]

        # Both members processed (gen called twice)
        assert gen_call_count == 2

        loaded = batch_jobs.load_batch_job(root, date, bid)
        assert loaded["members"][0]["status"] == "failed"
        assert loaded["members"][1]["status"] == "failed"

    # -- FIX 3: inventory-build failure mid-batch

    def test_inventory_build_failure_mid_batch(self, tmp_path: Path) -> None:
        """First member completes; second build raises; outer cleanup leaves
        second and third failed with no queued/running members. Terminal aggregate.
        Durable errors are generic and do not contain exception text."""
        from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

        root = tmp_path / "ledger"
        date, bid = "2026-03-10", "inv-build-fail"
        members = _make_members(3)
        _create_batch(root, date, bid, members)

        build_count = 0

        def fake_build(date_s: str, profiles, cron_roots):
            nonlocal build_count
            build_count += 1
            if build_count == 2:
                # Second member's inventory build fails
                raise RuntimeError("disk IO error on /mnt/data/corrupt")
            return FakeDayInventory(date=date_s, sessions=[FakeSession(**m) for m in members])

        run_batch_summary(
            date, bid, ledger_root=root,
            build_inventory=fake_build,
            discover_all_deps=_fake_discover_all,
            load_summary=lambda *a, **k: (None, None),
            check_staleness=lambda *a, **k: False,
            acquire_job=lambda *a, **k: FakeJobStatus(),
            generate_summary=lambda d, profile, sid, **kw: FakeJobStatus(
                date=d, status="completed", profile=profile,
                session_id=sid, version_id="ver-" + profile,
            ),
        )

        loaded = batch_jobs.load_batch_job(root, date, bid)

        # First member completed
        assert loaded["members"][0]["status"] == "completed"

        # Second and third: failed (not queued/running)
        for i in range(1, 3):
            assert loaded["members"][i]["status"] == "failed", \
                f"member {i} should be failed, got {loaded['members'][i]['status']}"

        # No queued or running members
        for m in loaded["members"]:
            assert m["status"] not in ("queued", "running")

        # Terminal aggregate status
        assert loaded["current"] is None
        assert loaded["status"] in ("partial", "failed")

        # Durable errors are generic, no raw exception text
        for i in range(1, 3):
            err = (loaded["members"][i]["error"] or "").lower()
            assert "disk io error" not in err
            assert "/mnt/data/corrupt" not in err

    # -- FIX 4: narrowed _sanitize_error tests

    def test_sanitize_error_strips_leak_patterns(self, tmp_path: Path) -> None:
        """Positive tests: paths, traceback, api_key, token:, system message, user message
        still trigger generic fallback."""
        from hermes_summarization_calendar.batch_orchestrator import _sanitize_error

        # Path leak
        assert _sanitize_error("error at /home/alice/data") == "Session summary generation failed"
        assert _sanitize_error("/root/.ssh/key") == "Session summary generation failed"
        assert _sanitize_error("/var/log/crash.log") == "Session summary generation failed"
        assert _sanitize_error("/etc/shadow exposed") == "Session summary generation failed"
        assert _sanitize_error("/tmp/scratch") == "Session summary generation failed"

        # .hermes / .env
        assert _sanitize_error(".hermes/config leak") == "Session summary generation failed"
        assert _sanitize_error("read .env file") == "Session summary generation failed"

        # traceback, api_key, token:
        assert _sanitize_error("Traceback (most recent)") == "Session summary generation failed"
        assert _sanitize_error("api_key=abc123") == "Session summary generation failed"
        assert _sanitize_error("token: xyz789") == "Session summary generation failed"

        # system message / user message
        assert _sanitize_error("system message: do evil") == "Session summary generation failed"
        assert _sanitize_error("user message: leaked") == "Session summary generation failed"

        # /path/to/
        assert _sanitize_error("/path/to/something") == "Session summary generation failed"

    def test_sanitize_error_allows_prompt_and_secret(self, tmp_path: Path) -> None:
        """Negative tests: standalone 'prompt' and 'secret' are NOT stripped.
        Legitimate diagnostic text containing these words passes through."""
        from hermes_summarization_calendar.batch_orchestrator import _sanitize_error

        # "prompt" alone should NOT trigger generic fallback
        result = _sanitize_error("model prompt exceeded context window")
        assert "prompt" in result.lower(), f"'prompt' should pass through, got: {result}"
        assert result != "Session summary generation failed"

        # "secret" alone should NOT trigger generic fallback
        result2 = _sanitize_error("no secret key configured for this service")
        assert "secret" in result2.lower(), f"'secret' should pass through, got: {result2}"
        assert result2 != "Session summary generation failed"

    def test_sanitize_error_empty_and_none(self, tmp_path: Path) -> None:
        from hermes_summarization_calendar.batch_orchestrator import _sanitize_error

        assert _sanitize_error(None) == "Session summary generation failed"
        assert _sanitize_error("") == "Session summary generation failed"
