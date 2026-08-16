"""Hermes Summarization Calendar -- dashboard plugin API routes.

Mounted at ``/api/plugins/summarization-calendar/`` by the Hermes dashboard plugin
loader.  Requires existing dashboard auth (handled by the middleware that
wraps all ``/api/plugins/...`` routes); no duplicate auth here.

Endpoints:
- GET  /health
- GET  /month
- GET  /day
- GET  /recap?date=YYYY-MM-DD
- POST /recap?date=YYYY-MM-DD
- GET  /recap/versions?date=YYYY-MM-DD
- POST /recap/rollback?date=YYYY-MM-DD&version=...
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import asdict
from datetime import date as _date
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field


# Hermes loads this file directly with ``spec_from_file_location`` and does not
# add dashboard/ to sys.path.  Load the sibling package explicitly rather than
# mutating the process-wide import path (which could shadow unrelated modules).
_PACKAGE_NAME = "hermes_summarization_calendar"
_PACKAGE_INIT = Path(__file__).resolve().parent / _PACKAGE_NAME / "__init__.py"
_existing_package = sys.modules.get(_PACKAGE_NAME)
if _existing_package is None:
    _package_spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        _PACKAGE_INIT,
        submodule_search_locations=[str(_PACKAGE_INIT.parent)],
    )
    if _package_spec is None or _package_spec.loader is None:
        raise ImportError(f"Cannot load {_PACKAGE_NAME} from {_PACKAGE_INIT}")
    _package_module = importlib.util.module_from_spec(_package_spec)
    sys.modules[_PACKAGE_NAME] = _package_module
    try:
        _package_spec.loader.exec_module(_package_module)
    except Exception:
        sys.modules.pop(_PACKAGE_NAME, None)
        raise
else:
    _existing_file = Path(getattr(_existing_package, "__file__", "")).resolve()
    if _existing_file != _PACKAGE_INIT.resolve():
        raise ImportError(
            f"Refusing {_PACKAGE_NAME} package collision: "
            f"loaded from {_existing_file}, expected {_PACKAGE_INIT.resolve()}"
        )

from hermes_summarization_calendar import __version__
from hermes_summarization_calendar.contract import (
    DailySession,
    DayCell,
    MonthInventory,
    HealthStatus,
    day_inventory_to_dict,
    health_to_dict,
    month_to_dict,
)
from hermes_summarization_calendar.inventory import (
    build_day_inventory,
    build_month_inventory,
    check_health,
    discover_all,
)

# Recap modules -- always imported; ImportError raises at load time which is
# fine since this plugin requires them.
from hermes_summarization_calendar.concurrency import (  # noqa: F401
    acquire_generation_slot,
    load_status as _load_job_status,
    recover_stale_locks,
)
from hermes_summarization_calendar.recap_orchestrator import check_recap_status, generate_recap
from hermes_summarization_calendar.recap_storage import (  # noqa: F401
    get_ledger_root,
    list_versions as _list_versions,
    recap_exists,
    rollback_to_version,
    validate_version_id,
)
from hermes_summarization_calendar.rollup_orchestrator import (
    build_rollup_inputs,
    check_rollup_status,
    generate_rollup,
)
from hermes_summarization_calendar.session_orchestrator import generate_session_summary
from hermes_summarization_calendar.session_storage import (
    artifact_key,
    check_session_staleness,
    list_rollup_versions,
    list_session_versions,
    load_rollup,
    load_session_summary,
    rollback_rollup,
    rollback_session_summary,
    session_summary_exists,
)
from hermes_summarization_calendar.summary_jobs import (
    acquire_rollup_job,
    acquire_session_job,
    fail_rollup_job,
    fail_session_job,
    load_rollup_job,
    load_session_job,
    recover_stale_jobs,
)
from hermes_summarization_calendar.batch_jobs import (
    create_batch_job,
    load_batch_job,
    list_batch_jobs,
    recover_stale_batch_jobs,
)
from hermes_summarization_calendar.batch_orchestrator import run_batch_summary

# Keep module reference for helper calls
import hermes_summarization_calendar.batch_jobs as batch_jobs

logger = logging.getLogger(__name__)

import json

router = APIRouter()

# Strict API identity patterns
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")

# Background worker thread pool for recap generation
_worker_pool: dict[str, threading.Thread] = {}
_worker_lock = threading.Lock()

# Flag to ensure startup recovery runs exactly once per process
_startup_done = False
_startup_lock = threading.Lock()

# Max concurrent summarization workers across all dates (prevents unbounded spawning)
_MAX_CONCURRENCY = 4


class RecapRequestBody(BaseModel):
    force_regenerate: bool = Field(default=False)


class BatchMember(BaseModel):
    profile: str
    session_id: str


class BatchRequestBody(BaseModel):
    sessions: list[BatchMember]
    regenerate_current: bool = False


# ---------------------------------------------------------------------------
# Startup hook -- recover stale locks (runs ONCE per process)
# ---------------------------------------------------------------------------

def _on_startup() -> None:
    """Recover stale running locks from prior Dashboard session.

    Guarded by a module-level flag so it runs exactly once, not on every
    /health hit (which could mis-mark live workers as failed).
    """
    global _startup_done
    with _startup_lock:
        if _startup_done:
            return
        try:
            recovered = recover_stale_locks()
            recovered.extend(recover_stale_jobs())
            recovered.extend(recover_stale_batch_jobs(get_ledger_root()))
        except Exception as exc:
            # Leave _startup_done false so a later request can retry.
            logger.warning(f"Stale lock recovery failed: {exc}")
            return
        _startup_done = True
        if recovered:
            logger.info(
                f"Recovered {len(recovered)} stale recap locks: {recovered}"
            )


def _ensure_startup() -> None:
    """Ensure startup recovery has been called (idempotent)."""
    if not _startup_done:
        _on_startup()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_date(date_str: str) -> None:
    """Validate strict ``YYYY-MM-DD`` format and actual calendar validity."""
    if not _DATE_RE.match(date_str):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format {date_str!r}, expected YYYY-MM-DD",
        )
    try:
        parts = date_str.split("-")
        _date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid calendar date {date_str!r}",
        )


def _validate_year_month(year: int, month: int) -> None:
    """Validate year/month for month grid endpoints."""
    if not (1 <= month <= 12):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid month {month}: must be 1-12",
        )
    if year < 2000 or year > 2100:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid year {year}: must be 2000-2100",
        )


def _validate_session_identity(profile: str, session_id: str) -> None:
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise HTTPException(status_code=400, detail="Invalid profile identity")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session identity")


def _find_canonical_session(
    sessions: list[DailySession], profile: str, session_id: str
) -> DailySession | None:
    for session in sessions:
        if session.profile == profile and session.session_id == session_id:
            return session
    return None


def _session_version_dict(version: Any) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "generated_at": version.generated_at,
        "source_fingerprint": version.source_fingerprint,
        "profile": version.profile,
        "session_id": version.session_id,
        "title": version.title,
    }


def _rollup_version_dict(version: Any) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "generated_at": version.generated_at,
        "source_fingerprint": version.source_fingerprint,
    }


def _remove_worker(pool_key: str) -> None:
    with _worker_lock:
        if _worker_pool.get(pool_key) is threading.current_thread():
            _worker_pool.pop(pool_key, None)


def _run_session_worker(
    date_str: str, profile: str, session_id: str, pool_key: str
) -> None:
    try:
        result = generate_session_summary(
            date_str, profile, session_id, slot_reserved=True
        )
        if result.status != "completed":
            logger.error("Session summary failed for %s: %s", pool_key, result.error)
    except Exception as exc:
        logger.exception("Unexpected session summary worker failure: %s", exc)
        try:
            fail_session_job(date_str, profile, session_id, str(exc))
        except Exception:
            pass
    finally:
        _remove_worker(pool_key)


def _run_rollup_worker(date_str: str, pool_key: str) -> None:
    try:
        result = generate_rollup(date_str, slot_reserved=True)
        if result.status != "completed":
            logger.error("Roll-up failed for %s: %s", date_str, result.error)
    except Exception as exc:
        logger.exception("Unexpected roll-up worker failure: %s", exc)
        try:
            fail_rollup_job(date_str, str(exc))
        except Exception:
            pass
    finally:
        _remove_worker(pool_key)


def _run_recap_worker(date_str: str) -> None:
    """Background worker that runs the full recap generation pipeline."""
    try:
        result = generate_recap(date=date_str)
        if result.status == "completed":
            logger.info(
                f"Recap generation completed for {date_str}: {result.version_id}"
            )
        else:
            logger.error(
                f"Recap generation failed for {date_str}: {result.error}"
            )
    except Exception as exc:
        logger.exception(f"Unexpected error in recap worker for {date_str}: {exc}")
    finally:
        with _worker_lock:
            if _worker_pool.get(date_str) is threading.current_thread():
                _worker_pool.pop(date_str, None)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict[str, Any]:
    """Plugin health check."""
    # Run startup hook on first health check
    try:
        _on_startup()
    except Exception:
        pass

    profiles, cron_roots = discover_all()
    sources, cron_readable = check_health(profiles, cron_roots)

    readable = sum(1 for s in sources if s.readable)
    unreadable_labels = [s.profile_label for s in sources if not s.readable]

    status = "ok" if readable > 0 else "degraded"
    health_obj = HealthStatus(
        status=status,
        plugin_name="summarization-calendar",
        version=__version__,
        profiles_discovered=len(profiles),
        readable_sources=readable,
        cron_readable=cron_readable,
        unreadable_sources=unreadable_labels,
        sources=sources,
    )
    return health_to_dict(health_obj)


# ---------------------------------------------------------------------------
# GET /month
# ---------------------------------------------------------------------------

@router.get("/month")
def month(
    year: int = Query(..., ge=2000, le=2100),
    month_param: int = Query(..., alias="month", ge=1, le=12),
) -> dict[str, Any]:
    """Return activity counts for every day in a calendar month.

    Includes ``has_recap`` and ``recap_stale`` flags derived from stored
    recaps without mutating source DBs.
    """
    _validate_year_month(year, month_param)
    profiles, cron_roots = discover_all()
    inventory = build_month_inventory(year, month_param, profiles, cron_roots)

    # Enrich each day cell with recap existence and staleness
    ledger_root = get_ledger_root()
    enriched_days: list[DayCell] = []

    for cell in inventory.days:
        rollup_raw, rollup_meta = load_rollup(cell.date, ledger_root)
        has_rollup = rollup_raw is not None and rollup_meta is not None
        has_legacy_recap = recap_exists(cell.date, ledger_root)
        has_recap = has_rollup or has_legacy_recap

        stale = False
        if has_rollup:
            try:
                stale = bool(
                    check_rollup_status(
                        cell.date, profiles, cron_roots, ledger_root
                    ).get("stale")
                )
            except Exception:
                pass
        elif has_legacy_recap:
            try:
                day_inv = build_day_inventory(cell.date, profiles, cron_roots)
                from hermes_summarization_calendar.recap_storage import check_staleness
                stale = check_staleness(
                    cell.date, day_inv.source_fingerprint, ledger_root
                )
            except Exception:
                pass

        enriched_days.append(DayCell(
            date=cell.date,
            active=cell.active,
            session_count=cell.session_count,
            cron_run_count=cell.cron_run_count,
            has_recap=has_recap,
            recap_stale=stale,
        ))

    return month_to_dict(MonthInventory(
        year=inventory.year,
        month=inventory.month,
        days=enriched_days,
    ))


# ---------------------------------------------------------------------------
# GET /day
# ---------------------------------------------------------------------------

@router.get("/day")
def day(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
) -> dict[str, Any]:
    """Return day metadata enriched with cheap per-session summary status."""
    _validate_date(date_str)
    profiles, cron_roots = discover_all()
    inventory = build_day_inventory(date_str, profiles, cron_roots)
    result = day_inventory_to_dict(inventory)
    ledger_root = get_ledger_root()
    for session in result["sessions"]:
        profile = session["profile"]
        session_id = session["session_id"]
        raw, meta = load_session_summary(date_str, profile, session_id, ledger_root)
        job = load_session_job(date_str, profile, session_id, ledger_root)
        exists = raw is not None and meta is not None
        session["summary_status"] = {
            "exists": exists,
            "stale": (
                check_session_staleness(
                    date_str,
                    profile,
                    session_id,
                    session["source_fingerprint"],
                    ledger_root,
                )
                if exists
                else False
            ),
            "version_id": meta.get("version_id") if meta else None,
            "generated_at": meta.get("generated_at") if meta else None,
            "job_status": asdict(job) if job else None,
        }
    return result


# ---------------------------------------------------------------------------
# GET /recap
# ---------------------------------------------------------------------------

@router.get("/recap")
def get_recap(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
) -> dict[str, Any]:
    """Check the status of a recap for a given date.

    Returns metadata and data if available, plus staleness flag.
    Never includes filesystem paths or raw transcripts.
    """
    _validate_date(date_str)

    try:
        profiles, cron_roots = discover_all()
        day_inv = build_day_inventory(date_str, profiles, cron_roots)
    except Exception:
        day_inv = None

    current_fp = day_inv.source_fingerprint if day_inv else ""
    ledger_root = get_ledger_root()
    result = check_recap_status(date_str, current_fp, ledger_root)

    # Keep recap rendering and rollback controls on one consistent snapshot.
    # This is metadata only: no paths or transcript content are exposed.
    if result.get("exists"):
        result["versions"] = [
            {
                "version_id": version.version_ts,
                "generated_at": version.generated_at,
                "source_fingerprint": version.source_fingerprint,
                "session_count": version.session_count,
                "cron_count": version.cron_count,
            }
            for version in _list_versions(date_str, ledger_root)
        ]

    return result


# ---------------------------------------------------------------------------
# POST /recap
# ---------------------------------------------------------------------------

@router.post("/recap", status_code=202)
def post_recap(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
    body: RecapRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    """Queue recap generation for a given date.

    Returns 202 with job status promptly; generation runs in background.
    If a recap exists and ``force_regenerate`` is not true, returns 400.
    If a generation is already running for the same date, returns 409.

    Body shape::

        {"force_regenerate": true}  # overwrite existing recap
    """
    _validate_date(date_str)

    # Ensure stale recovery has run before direct POST (not only /health)
    _ensure_startup()

    force = body.force_regenerate if body else False

    ledger_root = get_ledger_root()

    # Existing recap requires explicit force flag
    existing = recap_exists(date_str, ledger_root)
    if existing and not force:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "recap_already_exists",
                "message": f"Recap for {date_str} already exists. Set force_regenerate=true to overwrite.",
            },
        )

    # Check for concurrent generation (per-date 409)
    if not acquire_generation_slot(date_str, ledger_root):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "concurrent_request",
                "message": f"Recap generation for {date_str} is already in progress",
            },
        )

    # Construct the thread before reserving its pool entry.  The reservation is
    # inserted while holding the same lock as the capacity check, so pending
    # (not-yet-started) workers count toward the limit too.
    worker = threading.Thread(
        target=_run_recap_worker,
        args=(date_str,),
        daemon=True,
        name=f"recap-worker-{date_str}",
    )

    # Enforce global max concurrency bound across all dates and reserve a slot.
    with _worker_lock:
        active_count = len(_worker_pool)
        if active_count >= _MAX_CONCURRENCY:
            # Release the per-date slot we just acquired — worker will never run
            from hermes_summarization_calendar.concurrency import release_generation_slot
            try:
                release_generation_slot(date_str)
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "too_many_workers",
                    "message": f"Maximum {_MAX_CONCURRENCY} concurrent recap workers active. Try again shortly.",
                },
            )
        _worker_pool[date_str] = worker

    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        # Thread failed to start — remove pool entry and release durable slot
        with _worker_lock:
            if _worker_pool.get(date_str) is worker:
                _worker_pool.pop(date_str, None)
        from hermes_summarization_calendar.concurrency import release_generation_slot
        try:
            release_generation_slot(date_str)
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail={
                "error": "worker_start_failed",
                "message": f"Failed to start recap worker for {date_str}: {exc}",
            },
        )

    job_id = f"recap-{date_str}-{os.getpid()}"

    return {
        "status": "queued",
        "job_id": job_id,
    }


# ---------------------------------------------------------------------------
# GET /recap/versions
# ---------------------------------------------------------------------------

@router.get("/recap/versions")
def get_recap_versions(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
) -> dict[str, Any]:
    """List all archived versions of a recap for a given date."""
    _validate_date(date_str)

    versions = _list_versions(date_str, get_ledger_root())

    return {
        "date": date_str,
        "versions": [
            {
                "version_id": v.version_ts,
                "generated_at": v.generated_at,
                "source_fingerprint": v.source_fingerprint,
                "session_count": v.session_count,
                "cron_count": v.cron_count,
            }
            for v in versions
        ],
    }


# ---------------------------------------------------------------------------
# POST /recap/rollback
# ---------------------------------------------------------------------------

@router.post("/recap/rollback")
def post_recap_rollback(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
    version: str = Query(..., description="Version timestamp to restore (YYYYMMDDTHHmmssZ)"),
) -> dict[str, Any]:
    """Rollback a recap to a specific archived version.

    Mutating operation -- uses POST instead of GET.
    The current recap is archived before replacement.
    """
    _validate_date(date_str)

    # Validate version format using shared safe validator
    if not validate_version_id(version):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_version",
                "message": f"Invalid version ID {version!r}, expected YYYYMMDDTHHmmssZ_... safe format",
            },
        )

    restored = rollback_to_version(date_str, version, get_ledger_root())

    if restored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "version_not_found",
                "message": f"Version {version!r} not found for date {date_str}",
            },
        )

    return {
        "status": "restored",
        "version_id": restored.version_ts,
        "generated_at": restored.generated_at,
        "source_fingerprint": restored.source_fingerprint,
        "session_count": restored.session_count,
    }


# ---------------------------------------------------------------------------
# Per-session summary API
# ---------------------------------------------------------------------------

@router.get("/session-summary")
def get_session_summary(
    date_str: str = Query(..., alias="date"),
    profile: str = Query(...),
    session_id: str = Query(...),
) -> dict[str, Any]:
    _validate_date(date_str)
    _validate_session_identity(profile, session_id)
    profiles, cron_roots = discover_all()
    inventory = build_day_inventory(date_str, profiles, cron_roots)
    canonical = _find_canonical_session(inventory.sessions, profile, session_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Session not found for selected date")

    ledger_root = get_ledger_root()
    raw, meta = load_session_summary(date_str, profile, session_id, ledger_root)
    job = load_session_job(date_str, profile, session_id, ledger_root)
    exists = raw is not None and meta is not None
    return {
        "date": date_str,
        "profile": profile,
        "session_id": session_id,
        "title": canonical.title,
        "exists": exists,
        "stale": (
            check_session_staleness(
                date_str,
                profile,
                session_id,
                canonical.source_fingerprint,
                ledger_root,
            )
            if exists
            else False
        ),
        "data": raw,
        "meta": meta,
        "job_status": asdict(job) if job else None,
        "versions": [
            _session_version_dict(version)
            for version in list_session_versions(
                date_str, profile, session_id, ledger_root
            )
        ],
    }


@router.post("/session-summary", status_code=202)
def post_session_summary(
    date_str: str = Query(..., alias="date"),
    profile: str = Query(...),
    session_id: str = Query(...),
    body: RecapRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    _validate_date(date_str)
    _validate_session_identity(profile, session_id)
    _ensure_startup()
    profiles, cron_roots = discover_all()
    inventory = build_day_inventory(date_str, profiles, cron_roots)
    canonical = _find_canonical_session(inventory.sessions, profile, session_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Session not found for selected date")

    ledger_root = get_ledger_root()
    force = body.force_regenerate if body else False
    if session_summary_exists(date_str, profile, session_id, ledger_root) and not force:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "summary_already_exists",
                "message": "Session summary already exists; set force_regenerate=true",
            },
        )
    if acquire_session_job(date_str, profile, session_id, ledger_root) is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "concurrent_request", "message": "Summary job is already running or worker capacity is full"},
        )

    pool_key = "session:" + artifact_key(date_str, profile, session_id)
    worker = threading.Thread(
        target=_run_session_worker,
        args=(date_str, profile, session_id, pool_key),
        daemon=True,
        name=f"summary-worker-{pool_key[-12:]}",
    )
    with _worker_lock:
        if len(_worker_pool) >= _MAX_CONCURRENCY:
            fail_session_job(
                date_str, profile, session_id, "Worker capacity is full", ledger_root
            )
            raise HTTPException(status_code=503, detail="Worker capacity is full")
        _worker_pool[pool_key] = worker
    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        with _worker_lock:
            if _worker_pool.get(pool_key) is worker:
                _worker_pool.pop(pool_key, None)
        fail_session_job(date_str, profile, session_id, str(exc), ledger_root)
        raise HTTPException(status_code=500, detail="Failed to start summary worker")
    return {"status": "queued", "job_id": pool_key}


@router.get("/session-summary/versions")
def get_session_summary_versions(
    date_str: str = Query(..., alias="date"),
    profile: str = Query(...),
    session_id: str = Query(...),
) -> dict[str, Any]:
    _validate_date(date_str)
    _validate_session_identity(profile, session_id)
    return {
        "date": date_str,
        "profile": profile,
        "session_id": session_id,
        "versions": [
            _session_version_dict(version)
            for version in list_session_versions(
                date_str, profile, session_id, get_ledger_root()
            )
        ],
    }


@router.post("/session-summary/rollback")
def post_session_summary_rollback(
    date_str: str = Query(..., alias="date"),
    profile: str = Query(...),
    session_id: str = Query(...),
    version: str = Query(...),
) -> dict[str, Any]:
    _validate_date(date_str)
    _validate_session_identity(profile, session_id)
    if not validate_version_id(version):
        raise HTTPException(status_code=400, detail="Invalid version ID")
    _ensure_startup()
    ledger_root = get_ledger_root()
    job = load_session_job(date_str, profile, session_id, ledger_root)
    if job is not None and job.status == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot restore a session summary while generation is running",
        )
    restored = rollback_session_summary(
        date_str, profile, session_id, version, ledger_root
    )
    if restored is None:
        raise HTTPException(status_code=404, detail="Session summary version not found")
    return {"status": "restored", **_session_version_dict(restored)}


# ---------------------------------------------------------------------------
# Summary-only daily roll-up API
# ---------------------------------------------------------------------------

@router.get("/rollup")
def get_rollup(
    date_str: str = Query(..., alias="date"),
) -> dict[str, Any]:
    _validate_date(date_str)
    profiles, cron_roots = discover_all()
    result = check_rollup_status(
        date_str, profiles, cron_roots, get_ledger_root()
    )
    result["versions"] = [
        _rollup_version_dict(version)
        for version in list_rollup_versions(date_str, get_ledger_root())
    ]
    return result


@router.post("/rollup", status_code=202)
def post_rollup(
    date_str: str = Query(..., alias="date"),
    body: RecapRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    _validate_date(date_str)
    _ensure_startup()
    profiles, cron_roots = discover_all()
    ledger_root = get_ledger_root()
    current = check_rollup_status(date_str, profiles, cron_roots, ledger_root)
    force = body.force_regenerate if body else False
    if current.get("exists") and not force:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "rollup_already_exists",
                "message": "Roll-up already exists; set force_regenerate=true",
            },
        )
    inputs = build_rollup_inputs(date_str, profiles, cron_roots, ledger_root)
    if inputs.coverage_included == 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_summaries", "message": "No current session summaries are available"},
        )
    if acquire_rollup_job(date_str, ledger_root) is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "concurrent_request", "message": "Roll-up job is already running or worker capacity is full"},
        )

    pool_key = f"rollup:{date_str}"
    worker = threading.Thread(
        target=_run_rollup_worker,
        args=(date_str, pool_key),
        daemon=True,
        name=f"rollup-worker-{date_str}",
    )
    with _worker_lock:
        if len(_worker_pool) >= _MAX_CONCURRENCY:
            fail_rollup_job(date_str, "Worker capacity is full", ledger_root)
            raise HTTPException(status_code=503, detail="Worker capacity is full")
        _worker_pool[pool_key] = worker
    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        with _worker_lock:
            if _worker_pool.get(pool_key) is worker:
                _worker_pool.pop(pool_key, None)
        fail_rollup_job(date_str, str(exc), ledger_root)
        raise HTTPException(status_code=500, detail="Failed to start roll-up worker")
    return {"status": "queued", "job_id": pool_key}


@router.get("/rollup/versions")
def get_rollup_versions(
    date_str: str = Query(..., alias="date"),
) -> dict[str, Any]:
    _validate_date(date_str)
    return {
        "date": date_str,
        "versions": [
            _rollup_version_dict(version)
            for version in list_rollup_versions(date_str, get_ledger_root())
        ],
    }


@router.post("/rollup/rollback")
def post_rollup_rollback(
    date_str: str = Query(..., alias="date"),
    version: str = Query(...),
) -> dict[str, Any]:
    _validate_date(date_str)
    if not validate_version_id(version):
        raise HTTPException(status_code=400, detail="Invalid version ID")
    _ensure_startup()
    ledger_root = get_ledger_root()
    job = load_rollup_job(date_str, ledger_root)
    if job is not None and job.status == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot restore a roll-up while generation is running",
        )
    restored = rollback_rollup(date_str, version, ledger_root)
    if restored is None:
        raise HTTPException(status_code=404, detail="Roll-up version not found")
    return {"status": "restored", **_rollup_version_dict(restored)}


# ---------------------------------------------------------------------------
# Batch summary API routes
# ---------------------------------------------------------------------------

def _build_day_inventory_safe(date_str: str, ledger_root: Path) -> Any | None:
    """Build day inventory for validation, return None if unavailable."""
    try:
        profiles, cron_roots = discover_all()
        return build_day_inventory(date_str, profiles, cron_roots)
    except Exception:
        return None


def _run_batch_worker(
    date: str,
    batch_id: str,
    pool_key: str,
    ledger_root: Path,
) -> None:
    """Worker thread for batch summary generation."""
    try:
        result = run_batch_summary(date, batch_id, ledger_root=ledger_root)
        status = result.get("status")
        if status not in {"completed", "partial"}:
            error = result.get("error")
            if not error and status == "failed":
                failed = result.get("failed")
                error = (
                    f"{failed} member(s) failed"
                    if isinstance(failed, int)
                    else "batch returned failed status"
                )
            if not error:
                error = f"batch returned status {status or 'missing status'}"
            logger.error("Batch summary failed for %s: %s", pool_key, error)
    except Exception as exc:
        logger.exception("Unexpected batch worker failure for %s: %s", pool_key, exc)
        # Best-effort terminalize on unexpected error
        try:
            batch_job = load_batch_job(ledger_root, date, batch_id)
            if batch_job and batch_job["status"] not in {"completed", "partial", "failed"}:
                now = batch_jobs._now()
                batch_job["status"] = "failed"
                batch_job["finished_at"] = now
                batch_job["current"] = None
                for member in batch_job["members"]:
                    if member["status"] in ("queued", "running"):
                        member["status"] = "failed"
                        member["error"] = "Batch process terminated unexpectedly (interrupted)"
                        member["version_id"] = None
                batch_job["completed"] = sum(1 for m in batch_job["members"] if m["status"] == "completed")
                batch_job["failed"] = sum(1 for m in batch_job["members"] if m["status"] == "failed")
                batch_job["skipped"] = sum(1 for m in batch_job["members"] if m["status"] in ("skipped_current", "skipped_running"))
                batch_file = ledger_root / "batch-jobs" / date / f"{batch_id}.json"
                batch_jobs._atomic_write_json(batch_file, batch_job)
        except Exception:
            pass
    finally:
        _remove_worker(pool_key)


@router.post("/session-summary/batch", status_code=202)
def post_batch_summary(
    date_str: str = Query(..., alias="date", description="Calendar date in YYYY-MM-DD format"),
    body: BatchRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    """Create and queue a batch of session summaries.

    - Validates all members exist for the date
    - Checks shared _worker_pool capacity under lock
    - Creates durable batch through accepted storage API
    - Creates exactly one daemon coordinator thread
    - Returns 202 with persisted batch object
    """
    _validate_date(date_str)
    _ensure_startup()

    if body is None or not body.sessions:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_sessions",
                "message": "Request body must include non-empty sessions list",
            },
        )

    # Validate member count (1..100)
    members_list = body.sessions
    if len(members_list) < 1 or len(members_list) > 100:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_member_count",
                "message": "sessions must contain between 1 and 100 members",
            },
        )

    # Validate each member has profile and session_id
    for idx, member in enumerate(members_list):
        if not isinstance(member.profile, str) or not member.profile.strip():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_member",
                    "message": f"member at index {idx} has invalid or blank profile",
                },
            )
        if not isinstance(member.session_id, str) or not member.session_id.strip():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_member",
                    "message": f"member at index {idx} has invalid or blank session_id",
                },
            )

    # Check for duplicate composite identities
    seen = set()
    for member in members_list:
        composite = f"{member.profile}\0{member.session_id}"
        if composite in seen:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "duplicate_identity",
                    "message": f"Duplicate composite identity for profile={member.profile}, session_id={member.session_id}",
                },
            )
        seen.add(composite)

    ledger_root = get_ledger_root()

    # Build canonical day inventory once before accepting anything
    day_inventory = _build_day_inventory_safe(date_str, ledger_root)
    if day_inventory is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "inventory_unavailable",
                "message": "Failed to build day inventory",
            },
        )

    # Validate all identities exist in that date before creating batch
    for member in members_list:
        found = False
        for session in day_inventory.sessions:
            if session.profile == member.profile and session.session_id == member.session_id:
                found = True
                break
        if not found:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "identity_absent",
                    "message": f"Identity {member.profile}/{member.session_id} not found for date {date_str}",
                },
            )

    # Check capacity BEFORE creating batch
    with _worker_lock:
        active_count = len(_worker_pool)
        if active_count >= _MAX_CONCURRENCY:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "worker_capacity_full",
                    "message": f"Maximum {_MAX_CONCURRENCY} concurrent workers active. Try again shortly.",
                },
            )

    # Generate path-safe unpredictable batch ID
    batch_id = f"batch-{secrets.token_hex(8)}"

    # Create the members list for batch_jobs.create_batch_job
    batch_members = [{"profile": m.profile, "session_id": m.session_id} for m in members_list]

    # Create durable batch
    try:
        batch_job = create_batch_job(
            ledger_root,
            date_str,
            batch_id,
            batch_members,
            regenerate_current=bool(body.regenerate_current),
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "batch_creation_failed",
                "message": str(exc),
            },
        )

    # Now reserve a worker pool slot and create the coordinator thread
    pool_key = f"batch:{date_str}:{batch_id}"

    with _worker_lock:
        # Double-check capacity inside the same lock
        active_count = len(_worker_pool)
        if active_count >= _MAX_CONCURRENCY:
            # Rollback batch creation
            batch_file = ledger_root / "batch-jobs" / date_str / f"{batch_id}.json"
            try:
                batch_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "worker_capacity_full",
                    "message": f"Maximum {_MAX_CONCURRENCY} concurrent workers active. Try again shortly.",
                },
            )
        worker = threading.Thread(
            target=_run_batch_worker,
            args=(date_str, batch_id, pool_key, ledger_root),
            daemon=True,
            name=f"batch-coordinator-{batch_id}",
        )
        _worker_pool[pool_key] = worker

    # Try to start the worker
    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        # Thread start failed - remove pool entry and terminalize batch
        with _worker_lock:
            if _worker_pool.get(pool_key) is worker:
                _worker_pool.pop(pool_key, None)

        # Best-effort terminalize batch
        try:
            batch_file = ledger_root / "batch-jobs" / date_str / f"{batch_id}.json"
            if batch_file.is_file():
                data = json.loads(batch_file.read_text())
                now = batch_jobs._now()
                data["status"] = "failed"
                data["finished_at"] = now
                data["current"] = None
                for member in data["members"]:
                    if member["status"] in ("queued", "running"):
                        member["status"] = "failed"
                        member["error"] = "Failed to start batch worker"
                        member["version_id"] = None
                data["completed"] = sum(1 for m in data["members"] if m["status"] == "completed")
                data["failed"] = sum(1 for m in data["members"] if m["status"] == "failed")
                data["skipped"] = sum(1 for m in data["members"] if m["status"] in ("skipped_current", "skipped_running"))
                batch_jobs._atomic_write_json(batch_file, data)
        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail={
                "error": "worker_start_failed",
                "message": f"Failed to start batch worker: {exc}",
            },
        )

    # Return the batch job status (without raw content)
    return {
        "status": "queued",
        "batch_id": batch_id,
        "date": batch_job["date"],
        "total": batch_job["total"],
        "members": [
            {"profile": m["profile"], "session_id": m["session_id"], "status": m["status"]}
            for m in batch_job["members"]
        ],
        "created_at": batch_job["created_at"],
    }


@router.get("/session-summary/batch")
def get_batch_summary(
    date_str: str = Query(..., alias="date"),
    batch_id: str = Query(..., alias="batch_id"),
) -> dict[str, Any]:
    """Get exact durable status for a batch, or 404.

    - Validates date and batch_id via storage API safely
    - Returns exact durable status or 404
    - Invalid path identity -> 400, never traversal
    """
    _validate_date(date_str)

    # Validate batch_id format (alphanumeric with underscores/hyphens only)
    if not re.match(r"^[a-zA-Z0-9_-]+$", batch_id):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_batch_id",
                "message": "batch_id must contain only letters, digits, underscores, or hyphens",
            },
        )

    # Prevent path traversal - reject slashes and other dangerous chars
    if "/" in batch_id or "\\" in batch_id or ".." in batch_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_batch_id",
                "message": "batch_id contains disallowed characters",
            },
        )

    ledger_root = get_ledger_root()
    batch_job = load_batch_job(ledger_root, date_str, batch_id)

    if batch_job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Batch {batch_id} not found for date {date_str}",
            },
        )

    # Return sanitized status (no raw content)
    return {
        "status": batch_job["status"],
        "batch_id": batch_job["batch_id"],
        "date": batch_job["date"],
        "regenerate_current": batch_job.get("regenerate_current", False),
        "total": batch_job["total"],
        "completed": batch_job.get("completed", 0),
        "failed": batch_job.get("failed", 0),
        "skipped": batch_job.get("skipped", 0),
        "current": batch_job.get("current"),
        "created_at": batch_job["created_at"],
        "started_at": batch_job.get("started_at"),
        "finished_at": batch_job.get("finished_at"),
        "members": [
            {
                "profile": m["profile"],
                "session_id": m["session_id"],
                "status": m["status"],
                "error": m.get("error"),
                "version_id": m.get("version_id"),
            }
            for m in batch_job["members"]
        ],
    }


@router.get("/session-summary/batches")
def list_batch_summaries(
    date_str: str = Query(..., alias="date"),
    limit: int = Query(default=20, ge=1, le=20),
) -> dict[str, Any]:
    """Return object with date and batches list (max 20 newest).

    - Validates date strictly
    - limit must be bool-false int (not bool itself) in 1..20
    - Returns at most 20 newest statuses
    """
    _validate_date(date_str)

    # Strict limit check - bool cannot arrive through HTTP query
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 20:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_limit",
                "message": "limit must be an integer between 1 and 20",
            },
        )

    ledger_root = get_ledger_root()
    batches = list_batch_jobs(ledger_root, date_str, limit=limit)

    return {
        "date": date_str,
        "batches": [
            {
                "batch_id": b["batch_id"],
                "status": b["status"],
                "total": b["total"],
                "completed": b.get("completed", 0),
                "failed": b.get("failed", 0),
                "skipped": b.get("skipped", 0),
                "created_at": b["created_at"],
                "started_at": b.get("started_at"),
                "finished_at": b.get("finished_at"),
            }
            for b in batches
        ],
    }
