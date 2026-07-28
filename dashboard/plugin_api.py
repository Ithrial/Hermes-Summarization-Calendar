"""Hermes Daily Ledger -- dashboard plugin API routes.

Mounted at ``/api/plugins/daily-ledger/`` by the Hermes dashboard plugin
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
_PACKAGE_NAME = "hermes_daily_ledger"
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

from hermes_daily_ledger import __version__
from hermes_daily_ledger.contract import (
    DailySession,
    DayCell,
    MonthInventory,
    HealthStatus,
    day_inventory_to_dict,
    health_to_dict,
    month_to_dict,
)
from hermes_daily_ledger.inventory import (
    build_day_inventory,
    build_month_inventory,
    check_health,
    discover_all,
)

# Recap modules -- always imported; ImportError raises at load time which is
# fine since this plugin requires them.
from hermes_daily_ledger.concurrency import (  # noqa: F401
    acquire_generation_slot,
    load_status as _load_job_status,
    recover_stale_locks,
)
from hermes_daily_ledger.recap_orchestrator import check_recap_status, generate_recap
from hermes_daily_ledger.recap_storage import (  # noqa: F401
    get_ledger_root,
    list_versions as _list_versions,
    recap_exists,
    rollback_to_version,
    validate_version_id,
)
from hermes_daily_ledger.rollup_orchestrator import (
    build_rollup_inputs,
    check_rollup_status,
    generate_rollup,
)
from hermes_daily_ledger.session_orchestrator import generate_session_summary
from hermes_daily_ledger.session_storage import (
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
from hermes_daily_ledger.summary_jobs import (
    acquire_rollup_job,
    acquire_session_job,
    fail_rollup_job,
    fail_session_job,
    load_rollup_job,
    load_session_job,
    recover_stale_jobs,
)

logger = logging.getLogger(__name__)

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
        plugin_name="daily-ledger",
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
                from hermes_daily_ledger.recap_storage import check_staleness
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
            from hermes_daily_ledger.concurrency import release_generation_slot
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
        from hermes_daily_ledger.concurrency import release_generation_slot
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
