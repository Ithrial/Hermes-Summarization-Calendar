"""Legacy recap generation orchestrator.

This module handles the whole-day recap generation route (POST /recap).
It uses the new auxiliary_runner for LLM calls but retains the legacy
storage format in recap_storage for backward compatibility.

The old subprocess path has been replaced with Hermes' auxiliary.compression
seam: agent.auxiliary_client.call_llm(task="compression", messages=[...])
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .auxiliary_runner import AuxiliaryResult, run_auxiliary_compression
from .chunker import (
    build_synthesis_prompt,
    chunk_transcripts,
)
from .concurrency import (
    complete_generation,
    fail_generation,
    load_status,
    save_status,
    RecapJobStatus,
)
from .inventory import (
    ProfileSource,
    CronRoot,
    build_day_inventory,
    discover_all,
)
from .recap_storage import (
    check_staleness,
    load_recap,
    recap_exists,
    save_recap,
)
from .limits import MAX_MODEL_PROMPT_BYTES
from .recap_validator import (
    SessionIdentity,
    validate_summary_output,
    ValidationReport,
)
from .transcript import (
    collect_all_day_transcripts,
    SessionTranscript,
)

logger = logging.getLogger(__name__)


def _reconcile_inventory_transcripts(
    transcripts: list[SessionTranscript],
    inventory_sessions: list,
) -> list[SessionTranscript]:
    """Add metadata-only transcripts for inventoried sessions with no recap roles."""
    by_key = {(t.profile, t.session_id): t for t in transcripts}
    for session in inventory_sessions:
        key = (session.profile, session.session_id)
        if key not in by_key:
            by_key[key] = SessionTranscript(
                session_id=session.session_id,
                profile=session.profile,
                title=session.title,
                source=session.source,
                model=session.model,
                messages=[],
            )
    return [by_key[key] for key in sorted(by_key)]


def _chunk_expected_identities(chunk) -> list[SessionIdentity]:
    """Return the unique canonical identities represented in one chunk."""
    identities: dict[tuple[str, str], SessionIdentity] = {}
    for session in chunk.session_transcripts:
        identity = SessionIdentity(
            session_id=session["session_id"],
            title=session["title"],
            profile=session["profile"],
        )
        identities.setdefault(identity.composite_key, identity)
    return [identities[key] for key in sorted(identities)]


def _validate_chunk_result(result: AuxiliaryResult, chunk) -> ValidationReport:
    """Validate auxiliary output against the exact identities in its input chunk."""
    return validate_summary_output(
        {
            "session_summaries": result.session_summaries,
            "overall_recap": result.overall_recap,
            "cron_summary": result.cron_summary,
        },
        _chunk_expected_identities(chunk),
    )


def _merge_chunk_session_summaries(
    chunk_results: list[AuxiliaryResult],
    expected_identities: list[SessionIdentity],
) -> list[dict]:
    """Merge split-session summaries without delegating identity authority."""
    expected = {identity.composite_key: identity for identity in expected_identities}
    parts: dict[tuple[str, str], list[str]] = {key: [] for key in expected}

    for result in chunk_results:
        for item in result.session_summaries:
            key = ((item.get("profile") or "").strip(), (item.get("session_id") or "").strip())
            if key not in expected:
                raise ValueError(f"Unexpected chunk identity {key!r}")
            parts[key].append((item.get("summary") or "").strip())

    merged: list[dict] = []
    for key in sorted(expected):
        identity = expected[key]
        summaries = parts[key]
        if not summaries:
            raise ValueError(f"Missing chunk summary for identity {key!r}")
        if len(summaries) == 1:
            summary = summaries[0]
        else:
            summary = " | ".join(
                f"[{index + 1}/{len(summaries)}] {text}"
                for index, text in enumerate(summaries)
            )
        merged.append(
            {
                "profile": identity.profile,
                "session_id": identity.session_id,
                "title": identity.title,
                "summary": summary,
            }
        )
    return merged


def generate_recap(
    date: str,
    profiles: list[ProfileSource] | None = None,
    cron_roots: list[CronRoot] | None = None,
    safe_ceiling: int = MAX_MODEL_PROMPT_BYTES,
    ledger_root: Path | None = None,
    hermes_home: Path | None = None,
) -> RecapJobStatus:
    """Full recap generation pipeline for a single date.

    This uses the new auxiliary_runner for LLM calls instead of the legacy
    subprocess path. The storage format in recap_storage is retained
    for backward compatibility.

    Steps:
    1. Discover profiles and cron roots (or use supplied ones).
    2. Build day inventory to get source fingerprint.
    3. Collect all session transcripts.
    4. Chunk if needed under safe_ceiling.
    5. Run auxiliary runner for each chunk.
    6. Run final synthesis if multiple chunks.
    7. Validate output against expected identities.
    8. Save atomically with version archive.

    Parameters
    ----------
    date :
        Calendar date in YYYY-MM-DD format.
    profiles, cron_roots :
        Pre-discovered sources. If None, auto-discovered from hermes_home.
    safe_ceiling :
        Max bytes per chunk prompt.
    ledger_root :
        Override for recap storage root.
    hermes_home :
        Override for profile/cron discovery.

    Returns
    -------
    RecapJobStatus with status='completed' or 'failed'.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Step 1: Discover sources
        if profiles is None or cron_roots is None:
            discovered_profiles, discovered_cron_roots = discover_all(hermes_home)
            if profiles is None:
                profiles = discovered_profiles
            if cron_roots is None:
                cron_roots = discovered_cron_roots

        # Step 2: Build inventory for fingerprint
        from .dates import chicago_day_window_utc

        start_dt, end_dt = chicago_day_window_utc(date)
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        inventory = build_day_inventory(date, profiles, cron_roots)
        source_fingerprint = inventory.source_fingerprint

        # Check if activity exists (sessions or cron runs)
        total_sessions = len(inventory.sessions)
        if total_sessions == 0 and len(inventory.cron_runs) == 0:
            logger.info(f"No activity on {date} — nothing to recap")
            return fail_generation(
                date,
                "No sessions or cron runs found for this date",
                ledger_root,
            )

        # Step 3: Collect transcripts
        transcripts = collect_all_day_transcripts(profiles, start_ts, end_ts)
        transcripts = _reconcile_inventory_transcripts(transcripts, inventory.sessions)

        if not transcripts:
            logger.info(f"No eligible transcript messages on {date}")
            return fail_generation(
                date,
                "No active user/assistant/tool messages found for this date",
                ledger_root,
            )

        # Build expected identities from inventory (ground truth)
        expected_identities = [
            SessionIdentity(
                session_id=s.session_id,
                title=s.title,
                profile=s.profile,
            )
            for s in inventory.sessions
        ]

        # Step 4: Chunk if needed
        chunks = chunk_transcripts(transcripts, safe_ceiling, date_str=date)

        if not chunks:
            return fail_generation(date, "No valid chunks produced", ledger_root)

        # Step 5: Run auxiliary runner for each chunk
        chunk_results: list[AuxiliaryResult] = []
        first_chunk_model: str | None = None
        synthesis_model: str | None = None
        for i, chunk in enumerate(chunks):
            logger.info(
                f"Running auxiliary chunk {i + 1}/{len(chunks)} for {date}"
            )
            result = run_auxiliary_compression(
                prompt=chunk.prompt_text,
                ledger_root=ledger_root,
            )
            if first_chunk_model is None:
                first_chunk_model = getattr(result, "response_model", None)

            if result.error:
                logger.error(f"Chunk {i + 1} failed: {result.error}")
                return fail_generation(
                    date,
                    f"Chunk {i + 1}/{len(chunks)} failed: {result.error}",
                    ledger_root,
                )

            chunk_report = _validate_chunk_result(result, chunk)
            if not chunk_report.valid:
                error_msg = "; ".join(chunk_report.errors)
                logger.error(f"Chunk {i + 1} validation failed: {error_msg}")
                return fail_generation(
                    date,
                    f"Chunk {i + 1}/{len(chunks)} validation failed: {error_msg}",
                    ledger_root,
                )

            chunk_results.append(result)

        # Step 6: Final synthesis if multiple chunks
        all_session_summaries = _merge_chunk_session_summaries(
            chunk_results,
            expected_identities,
        )
        overall_recap_parts: list[str] = []
        cron_summary_parts: list[str] = []

        if len(chunk_results) == 1:
            # Single chunk — use directly
            result = chunk_results[0]
            overall_recap_parts.append(result.overall_recap)
            if result.cron_summary:
                cron_summary_parts.append(result.cron_summary)
        else:
            # Multi-chunk — synthesis may improve prose, but the deterministic
            # merge above remains the sole authority for session membership.
            synch_prompt = build_synthesis_prompt(
                [{"session_summaries": cr.session_summaries, "cron_summary": cr.cron_summary} for cr in chunk_results],
                date,
            )

            synthesis_result = run_auxiliary_compression(
                prompt=synch_prompt,
                ledger_root=ledger_root,
            )
            synthesis_model = getattr(synthesis_result, "response_model", None)

            if synthesis_result.error:
                logger.error(f"Synthesis failed: {synthesis_result.error}")
                overall_recap_parts.append(
                    " ".join(cr.overall_recap for cr in chunk_results)
                )
            else:
                overall_recap_parts.append(synthesis_result.overall_recap)

            # Merge cron summaries from chunks
            for cr in chunk_results:
                if cr.cron_summary:
                    cron_summary_parts.append(cr.cron_summary)

        # Build cron summary from inventory
        if not cron_summary_parts and inventory.cron_runs:
            cron_lines = []
            for run in inventory.cron_runs:
                cron_lines.append(
                    f"Job '{run.job_name}' (ID: {run.job_id}) "
                    f"— status: {run.status}"
                )
            cron_summary_parts.append(" ".join(cron_lines))

        overall_recap = " ".join(overall_recap_parts)
        cron_summary = " ".join(cron_summary_parts).strip()

        # Step 7: Validate output
        recap_data = {
            "session_summaries": all_session_summaries,
            "overall_recap": overall_recap,
            "cron_summary": cron_summary,
        }

        report = validate_summary_output(recap_data, expected_identities)

        if not report.valid:
            error_msg = "; ".join(report.errors)
            logger.error(f"Validation failed for {date}: {error_msg}")
            return fail_generation(date, f"Validation failed: {error_msg}", ledger_root)

        if report.warnings:
            logger.warning(
                f"Validation warnings for {date}: {'; '.join(report.warnings)}"
            )

        # Use actual response model from orchestration:
        # - Single chunk: use first chunk result's model
        # - Multi-chunk with synthesis: use synthesis result's model
        # - Multi-chunk without synthesis: use first chunk result's model
        model = ""
        if len(chunk_results) == 1:
            model = first_chunk_model or ""
        else:
            # Multi-chunk: prefer synthesis model if successful, else first chunk
            model = synthesis_model or first_chunk_model or ""

        # Step 8: Save atomically
        saved_version = save_recap(
            date=date,
            data=recap_data,
            source_fingerprint=source_fingerprint,
            generated_at=now,
            profile="auxiliary.compression",
            model=model,
            ledger_root=ledger_root,
        )

        return complete_generation(
            date=date,
            version_id=saved_version.version_ts,
            ledger_root=ledger_root,
        )

    except Exception as exc:
        logger.exception(f"Recap generation failed for {date}: {exc}")
        error_msg = str(exc)[:500]
        return fail_generation(date, f"Generation error: {error_msg}", ledger_root)


def check_recap_status(
    date: str,
    current_fingerprint: str = "",
    ledger_root: Path | None = None,
) -> dict:
    """Check the status and staleness of a recap for a date.

    Returns a dict suitable for GET /recap response (no filesystem paths).
    """
    exists = recap_exists(date, ledger_root)

    if not exists:
        # Check durable job status
        job_status = load_status(date, ledger_root)
        return {
            "date": date,
            "exists": False,
            "job_status": {
                "status": job_status.status if job_status else "idle",
                "started_at": getattr(job_status, "started_at", None),
                "error": getattr(job_status, "error", None) if job_status and job_status.status == "failed" else None,
            } if job_status else {
                "status": "idle",
                "started_at": None,
                "error": None,
            },
            "stale": False,
        }

    # Recap exists — check staleness
    stale = check_staleness(date, current_fingerprint, ledger_root)

    raw, meta = load_recap(date, ledger_root)
    md = None
    try:
        from .recap_storage import load_recap_markdown
        md = load_recap_markdown(date, ledger_root)
    except Exception:
        pass

    # Never include filesystem paths in response
    result_meta = {}
    if meta:
        result_meta = {k: v for k, v in meta.items()
                       if k not in ("previous_version_path",)}

    return {
        "date": date,
        "exists": True,
        "meta": result_meta,
        "data": raw or {},
        "stale": stale,
    }
