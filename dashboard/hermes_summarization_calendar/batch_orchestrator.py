"""Sequential batch summary coordinator.

Processes an already-created durable batch strictly in stored order through the
existing per-session summary orchestrator.  Designed to be called from a single
background thread by the plugin API; does not own any global worker pool and
creates no auxiliary threads.

All external dependencies are injectable for deterministic testing.  Default
production callables wire into the existing real functions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, cast

from .batch_jobs import (
    finalize_batch_job,
    load_batch_job,
    start_batch_job,
    update_batch_member,
)
from .inventory import (
    build_day_inventory as _real_build_day_inventory,
    discover_all as _real_discover_all,
)
from .session_storage import (
    check_session_staleness as _real_check_session_staleness,
    load_session_summary as _real_load_session_summary,
)
from .summary_jobs import (
    SummaryJobStatus,
    acquire_session_job as _real_acquire_session_job,
    fail_session_job as _real_fail_session_job,
    load_session_job as _real_load_session_job,
)
from .session_orchestrator import generate_session_summary as _real_generate_session_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases for injectable dependencies
# ---------------------------------------------------------------------------

BuildInventoryFn = Callable[..., Any]
DiscoverAllFn = Callable[..., tuple[list[Any], list[Any]]]
LoadSummaryFn = Callable[..., tuple[dict[str, Any] | None, dict[str, Any] | None]]
CheckStalenessFn = Callable[..., bool]
AcquireJobFn = Callable[..., SummaryJobStatus | None]
LoadJobFn = Callable[..., SummaryJobStatus | None]
GenerateSummaryFn = Callable[..., SummaryJobStatus]
FailSessionJobFn = Callable[[str, str, str, str, Path | None], SummaryJobStatus]

# ---------------------------------------------------------------------------
# Generic sanitized error messages (never leak internals)
# ---------------------------------------------------------------------------

_ERR_SESSION_UNAVAILABLE = "Session is no longer available for this date"
_ERR_CAPACITY_FULL = "Summary worker capacity is currently full"
_ERR_GENERATION_FAILED = "Session summary generation failed"
_ERR_COORDINATOR_TERMINATED = "Batch coordinator terminated unexpectedly"


def _find_session(
    inventory: Any, profile: str, session_id: str
) -> Any | None:
    """Find a session in the inventory by composite identity."""
    for session in getattr(inventory, "sessions", []):
        if (getattr(session, "profile", None) == profile
                and getattr(session, "session_id", None) == session_id):
            return session
    return None


def _sanitize_error(raw: str | None) -> str:
    """Return a bounded sanitized error string safe for durable storage.

    Strips anything that looks like paths, keys, tracebacks, or raw content.
    Falls back to generic message if the input contains leak markers.
    """
    if not raw:
        return _ERR_GENERATION_FAILED

    # Collapse whitespace
    sanitized = " ".join(raw.strip().split())

    # Check for known leak patterns; if found, use generic fallback
    # Keep path/traceback/api_key/token markers and explicit system/user message markers.
    # Do NOT strip standalone "prompt" or "secret" — those are common in legitimate diagnostics.
    leak_patterns = [
        "/home/", "/root/", "/var/", "/etc/", "/tmp/",
        ".hermes/", ".env",
        "traceback", "api_key", "token:",
        "system message", "user message",
        "/path/to/",
    ]
    lower = sanitized.lower()
    if any(pat in lower for pat in leak_patterns):
        return _ERR_GENERATION_FAILED

    # Cap length
    if len(sanitized) > 500:
        sanitized = sanitized[:497] + "..."
    return sanitized


def run_batch_summary(
    date: str,
    batch_id: str,
    *,
    ledger_root: Path | None = None,
    # Injectable dependencies (defaults = production functions)
    build_inventory: BuildInventoryFn | None = None,
    discover_all_deps: DiscoverAllFn | None = None,
    load_summary: LoadSummaryFn | None = None,
    check_staleness: CheckStalenessFn | None = None,
    acquire_job: AcquireJobFn | None = None,
    load_job: LoadJobFn | None = None,
    generate_summary: GenerateSummaryFn | None = None,
    fail_session_job_dep: FailSessionJobFn | None = None,
) -> dict[str, Any]:
    """Process a durable batch sequentially through the per-session orchestrator.

    Parameters
    ----------
    date : str
        Calendar date in YYYY-MM-DD format.
    batch_id : str
        The batch job identifier.
    ledger_root : Path or None
        Root directory for ledger storage.
    build_inventory : callable, optional
        Build day inventory from (date_str, profiles, cron_roots).
    load_summary : callable, optional
        Load session summary returning (raw_data, metadata) tuple.
    check_staleness : callable, optional
        Check staleness returning bool.
    acquire_job : callable, optional
        Acquire individual session job slot.
    load_job : callable, optional
        Load existing individual session job status.
    generate_summary : callable, optional
        Generate session summary with slot_reserved kwarg support.

    Returns
    -------
    dict
        The finalized batch job status object.

    Raises
    ------
    ValueError
        If the batch is missing or has the wrong date. A terminal batch is
        returned unchanged so a repeated coordinator call is idempotent.
    """
    # Resolve injectable defaults
    if build_inventory is None:
        build_inventory = _real_build_day_inventory
    if discover_all_deps is None:
        discover_all_deps = _real_discover_all
    if load_summary is None:
        load_summary = _real_load_session_summary
    if check_staleness is None:
        check_staleness = _real_check_session_staleness
    if acquire_job is None:
        acquire_job = _real_acquire_session_job
    if load_job is None:
        load_job = _real_load_session_job
    if generate_summary is None:
        generate_summary = _real_generate_session_summary
    if fail_session_job_dep is None:
        fail_session_job_dep = _real_fail_session_job

    # Step 1: Load and start the durable batch
    batch_job = load_batch_job(ledger_root, date, batch_id)
    if batch_job is None:
        raise ValueError(f"batch job {batch_id} not found for {date}")
    if batch_job["status"] in {"completed", "partial", "failed"}:
        # A retry after another coordinator finalized the batch is a safe
        # read-only no-op. Do not re-run members or manufacture a failure.
        return batch_job

    try:
        started = start_batch_job(ledger_root, date, batch_id)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    members = started["members"]
    regenerate_current = started.get("regenerate_current", False)

    # Track which member index we're on for cleanup
    processed_up_to = -1  # index of last successfully handled member

    try:
        # Step 2: Process members strictly in stored order, one at a time
        for idx, member in enumerate(members):
            profile = member["profile"]
            session_id = member["session_id"]

            # Step 3: Re-evaluate live inventory before every item
            try:
                profiles, cron_roots = discover_all_deps()
                inventory = build_inventory(date, profiles, cron_roots)
            except Exception:
                logger.exception(
                    "Failed to build inventory for %s during batch %s", date, batch_id
                )
                # If we can't even get inventory, mark remaining members failed
                for mi in range(idx, len(members)):
                    try:
                        update_batch_member(
                            ledger_root, date, batch_id,
                            members[mi]["profile"], members[mi]["session_id"],
                            "failed", error=_ERR_COORDINATOR_TERMINATED,
                        )
                    except (ValueError, OSError):
                        pass
                break

            # Step 4: Check if session vanished from inventory
            session = _find_session(inventory, profile, session_id)
            if session is None:
                try:
                    update_batch_member(
                        ledger_root, date, batch_id,
                        profile, session_id,
                        "failed", error=_ERR_SESSION_UNAVAILABLE,
                    )
                except (ValueError, OSError):
                    pass
                processed_up_to = idx
                continue

            # Step 5: Check if individual session job is currently running
            existing_job = load_job(date, profile, session_id, ledger_root)
            if existing_job is not None and existing_job.status == "running":
                try:
                    update_batch_member(
                        ledger_root, date, batch_id,
                        profile, session_id,
                        "skipped_running",
                    )
                except (ValueError, OSError):
                    pass
                processed_up_to = idx
                continue

            # Step 6: Determine summary status from canonical storage
            need_generate = False
            try:
                raw_data, meta = load_summary(date, profile, session_id, ledger_root)
                if raw_data is None or meta is None:
                    # Missing artifact -> generate
                    need_generate = True
                else:
                    current_fp = getattr(session, "source_fingerprint", "")
                    if check_staleness(date, profile, session_id, current_fp, ledger_root):
                        # Stale artifact -> generate (preserving existing immutable version)
                        need_generate = True
                    elif not regenerate_current:
                        # Current artifact -> skip
                        try:
                            update_batch_member(
                                ledger_root, date, batch_id,
                                profile, session_id,
                                "skipped_current",
                            )
                        except (ValueError, OSError):
                            pass
                        processed_up_to = idx
                        continue
                    else:
                        # Current + regenerate_current -> generate
                        need_generate = True
            except Exception:
                # If we can't determine status conservatively, try to generate
                need_generate = True

            if not need_generate:
                # Safety fallback - should be covered above but be explicit
                processed_up_to = idx
                continue

            # Step 7: Reserve the individual job atomically
            reserved = acquire_job(date, profile, session_id, ledger_root)
            if reserved is None:
                # Re-read individual job to check race condition
                recheck = load_job(date, profile, session_id, ledger_root)
                if recheck is not None and recheck.status == "running":
                    try:
                        update_batch_member(
                            ledger_root, date, batch_id,
                            profile, session_id,
                            "skipped_running",
                        )
                    except (ValueError, OSError):
                        pass
                    processed_up_to = idx
                    continue
                else:
                    # True capacity failure
                    try:
                        update_batch_member(
                            ledger_root, date, batch_id,
                            profile, session_id,
                            "failed", error=_ERR_CAPACITY_FULL,
                        )
                    except (ValueError, OSError):
                        pass
                    processed_up_to = idx
                    continue

            # Mark member running before generation
            try:
                update_batch_member(
                    ledger_root, date, batch_id,
                    profile, session_id,
                    "running",
                )
            except (ValueError, OSError):
                pass

            try:
                # Step 8: Call generate_session_summary synchronously
                result = cast(
                    SummaryJobStatus,
                    generate_summary(
                        date, profile, session_id,
                        slot_reserved=True,
                        ledger_root=ledger_root,
                    ),
                )

                # Step 9/10: Handle completed vs failed result
                if result.status == "completed":
                    version_id = result.version_id or ""
                    try:
                        update_batch_member(
                            ledger_root, date, batch_id,
                            profile, session_id,
                            "completed", version_id=version_id,
                        )
                    except (ValueError, OSError):
                        pass
                elif result.status == "failed":
                    # Use the job's sanitized error or generic fallback
                    err = result.error
                    if not err:
                        err = _ERR_GENERATION_FAILED
                    else:
                        err = _sanitize_error(err)
                    try:
                        update_batch_member(
                            ledger_root, date, batch_id,
                            profile, session_id,
                            "failed", error=err,
                        )
                    except (ValueError, OSError):
                        pass

            except Exception as exc:
                # Step 11: Unexpected exception -> generic durable error
                logger.exception(
                    "Unexpected error generating summary for %s/%s in batch %s",
                    profile, session_id, batch_id,
                )
                # Best-effort release the reserved slot via fail_session_job
                try:
                    fail_session_job_dep(
                        date, profile, session_id,
                        _ERR_GENERATION_FAILED, ledger_root,
                    )
                except Exception:
                    pass
                generic_err = _ERR_GENERATION_FAILED
                try:
                    update_batch_member(
                        ledger_root, date, batch_id,
                        profile, session_id,
                        "failed", error=generic_err,
                    )
                except (ValueError, OSError):
                    pass

            processed_up_to = idx

    except (KeyboardInterrupt, SystemExit):
        # Step 13: Never suppress; cleanup + re-raise
        raise
    except Exception as exc:
        logger.exception(
            "Batch coordinator failed during processing of batch %s", batch_id
        )
        # Best-effort cleanup on unexpected outer failure handled below
    finally:
        # Step 13 (partial): Best-effort mark every unfinished member failed
        for mi in range(processed_up_to + 1, len(members)):
            m = members[mi]
            try:
                update_batch_member(
                    ledger_root, date, batch_id,
                    m["profile"], m["session_id"],
                    "failed", error=_ERR_COORDINATOR_TERMINATED,
                )
            except (ValueError, OSError):
                pass

        # Step 12: Finalize using accepted batch storage derivation
        try:
            finalized = finalize_batch_job(ledger_root, date, batch_id)
            return finalized
        except (ValueError, OSError) as finalize_err:
            logger.error(
                "Failed to finalize batch %s: %s", batch_id, finalize_err
            )
            # Return whatever we can load
            fallback = load_batch_job(ledger_root, date, batch_id)
            return fallback or {"error": str(finalize_err)}
