"""Tests for per-session summary identity-is-deterministic contract.

This regression addresses the observed failure where:
- Exact job: date 2026-07-27, canonical identity ('default', '20260727_113013_426899')
- One-session transcript produced 18 chunks at 49,152-byte ceiling
- Model returned multiple session_summaries entries, duplicated identity,
  and hallucinated adjacent identity 20260727_113012_426899
- Source DB has one row for ...113013..., zero rows for ...113012...
- Transcript contained legacy session_summaries/identity examples

Required outcome:
- Per-session summarization returns CONTENT ONLY (summary + key_points)
- Canonical identity (profile, session_id, title) is server-owned authority
- Model-supplied identity fields are strictly rejected
- No duplicate identities, no hallucinated adjacent IDs
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from hermes_daily_ledger.auxiliary_runner import AuxiliaryResult
from hermes_daily_ledger.chunker import _build_chunk_prompt, _build_session_chunk_prompt
from hermes_daily_ledger.contract import DailySession
from hermes_daily_ledger.recap_orchestrator import (
    _merge_chunk_session_summaries,
    _reconcile_inventory_transcripts,
)
from hermes_daily_ledger.recap_validator import SessionIdentity, validate_summary_output
from hermes_daily_ledger.session_orchestrator import (
    _build_reduction_prompt as sop_build_reduction_prompt,
    _validated_item,
)
from hermes_daily_ledger.transcript import SessionTranscript, TranscriptMessage


def _daily(session_id: str, title: str) -> DailySession:
    return DailySession(
        session_id=session_id,
        profile="default",
        source="cli",
        model="auxiliary.compression",
        title=title,
        message_count=1,
        tool_call_count=0,
    )


def _transcript(session_id: str, title: str) -> SessionTranscript:
    return SessionTranscript(
        session_id=session_id,
        profile="default",
        source="cli",
        model="auxiliary.compression",
        title=title,
        messages=[TranscriptMessage(role="user", content="work")],
    )


# ====================================================================
# Section 1: Chunker prompts must use real newlines, not escaped sequences
# ====================================================================


def test_generic_chunk_prompt_uses_real_newlines():
    """Generic multi-session chunk prompt must use real \\n, not escaped \\\\n."""
    transcripts = [{
        "session_id": "test-1",
        "profile": "default",
        "title": "Test Session",
        "source": "cli",
        "model": "auxiliary.compression",
        "messages": [{"role": "user", "content": "Hello"}],
    }]
    prompt = _build_chunk_prompt(transcripts, "2026-07-27")

    # Must contain real newlines, not literal backslash-n sequences
    assert "\\n" not in prompt, "Prompt must use real newlines, not escaped sequences"
    assert "\n" in prompt, "Prompt must contain real newlines"


def test_session_chunk_prompt_uses_real_newlines():
    """Per-session chunk prompt must use real \\n, not escaped \\\\n."""
    transcripts = [{
        "session_id": "test-1",
        "profile": "default",
        "title": "Test Session",
        "source": "cli",
        "model": "auxiliary.compression",
        "messages": [{"role": "user", "content": "Hello"}],
    }]
    prompt = _build_session_chunk_prompt(transcripts, "2026-07-27")

    # Must contain real newlines, not literal backslash-n sequences
    assert "\\n" not in prompt, "Prompt must use real newlines, not escaped sequences"
    assert "\n" in prompt, "Prompt must contain real newlines"


def test_reduction_prompt_uses_real_newlines():
    """Reduction prompt must use real \\n, not escaped \\\\n."""
    canonical = _daily("test-1", "Test Title")
    prompt = sop_build_reduction_prompt("2026-07-27", canonical, ["seg1", "seg2"])

    # Must contain real newlines, not literal backslash-n sequences
    assert "\\n" not in prompt, "Prompt must use real newlines, not escaped sequences"
    assert "\n" in prompt, "Prompt must contain real newlines"


# ====================================================================
# Section 2: Per-session prompts request content-only output contract
# ====================================================================


def test_generic_chunk_prompt_requests_full_format():
    """Generic chunk prompt for multi-session recap requests full composite identity."""
    transcripts = [{
        "session_id": "test-1",
        "profile": "default",
        "title": "Test Session",
        "source": "cli",
        "model": "auxiliary.compression",
        "messages": [{"role": "user", "content": "Hello"}],
    }]
    prompt = _build_chunk_prompt(transcripts, "2026-07-27")

    # Must include identity fields in shape example
    assert '"profile":' in prompt, "Generic prompt must include profile in shape"
    assert '"session_id":' in prompt, "Generic prompt must include session_id in shape"
    assert '"title":' in prompt, "Generic prompt must include title in shape"
    assert "'session_summaries'" in prompt or '"session_summaries"' in prompt


def test_session_chunk_prompt_requests_content_only():
    """Per-session chunk prompt requests ONLY summary + key_points, NO identity fields."""
    transcripts = [{
        "session_id": "test-1",
        "profile": "default",
        "title": "Test Session",
        "source": "cli",
        "model": "auxiliary.compression",
        "messages": [{"role": "user", "content": "Hello"}],
    }]
    prompt = _build_session_chunk_prompt(transcripts, "2026-07-27")

    # Must explicitly request content-only shape (summary + key_points only)
    assert '"summary":' in prompt, "Must request summary in output"
    assert '"key_points":' in prompt, "Must request key_points in output"

    # Must NOT mention forbidden identity/schema terms in instruction section (before LEDGER_DATA_BEGIN)
    instruction_section = prompt.split("LEDGER_DATA_BEGIN")[0]
    forbidden_terms = ["session_summaries", "overall_recap", "profile", "session_id", "title"]
    for term in forbidden_terms:
        assert term not in instruction_section, f"Prompt must not mention forbidden term: {term}"

    # Must mention server-owned to indicate identity is server-managed
    assert "server-owned" in prompt.lower(), "Must indicate identity is server-owned"


def test_reduction_prompt_requests_content_only():
    """Reduction prompt requests ONLY summary + key_points, NO identity fields."""
    canonical = _daily("test-1", "Test Title")
    prompt = sop_build_reduction_prompt("2026-07-27", canonical, ["seg1", "seg2"])

    # Must explicitly request content-only shape
    assert '"summary"' in prompt and '"key_points"' in prompt, (
        "Must request summary and key_points in output"
    )

    # Must NOT mention forbidden identity/schema terms in instruction section
    instruction_section = prompt.split("LEDGER_DATA_BEGIN")[0]
    forbidden_terms = ["session_summaries", "overall_recap", "profile", "session_id", "title"]
    for term in forbidden_terms:
        assert term not in instruction_section, f"Prompt must not mention forbidden term: {term}"

    # Must indicate identity is server-owned
    assert "server-owned" in prompt.lower(), "Must indicate identity is server-owned"


# ====================================================================
# Section 3: Validator accepts full format (for generic recap)
# ====================================================================


def test_validator_accepts_full_format_for_generic_recap():
    """Generic validator accepts full format with identity fields (for daily recaps)."""
    expected_identities = [
        SessionIdentity(
            session_id="test-1",
            title="Test Session",
            profile="default",
        )
    ]

    # Full format with identity fields (used by generic recap/orchestration)
    full_format_output = {
        "session_summaries": [{
            "profile": "default",
            "session_id": "test-1",
            "title": "Test Session",
            "summary": "The summary content",
            "key_points": ["point one"],
        }],
        "overall_recap": "Daily recap",
    }

    report = validate_summary_output(full_format_output, expected_identities)

    # Must PASS - full format is correct for generic recap
    assert report.valid is True, (
        "Full format output (with identity fields) must be valid for generic recap"
    )
    assert len(report.errors) == 0, f"Unexpected errors: {report.errors}"


def test_validator_rejects_missing_identity_in_full_format():
    """Generic validator rejects full format with missing identity fields."""
    expected_identities = [
        SessionIdentity(
            session_id="test-1",
            title="Test Session",
            profile="default",
        )
    ]

    # Full format missing profile (required for composite identity)
    invalid_full_format = {
        "session_summaries": [{
            "session_id": "test-1",
            "title": "Test Session",
            "summary": "The summary content",
            "key_points": ["point one"],
        }],
        "overall_recap": "Daily recap",
    }

    report = validate_summary_output(invalid_full_format, expected_identities)

    assert report.valid is False, "Must reject full format missing profile"
    assert any("profile" in err.lower() for err in report.errors), (
        "Error must mention missing profile"
    )


def test_validator_rejects_duplicate_identity_in_full_format():
    """Generic validator rejects full format with duplicate composite identity."""
    expected_identities = [
        SessionIdentity(
            session_id="test-1",
            title="Test Session",
            profile="default",
        )
    ]

    # Full format with duplicate identity
    duplicate_output = {
        "session_summaries": [{
            "profile": "default",
            "session_id": "test-1",
            "title": "Test Session",
            "summary": "Summary 1",
            "key_points": [],
        }, {
            "profile": "default",
            "session_id": "test-1",
            "title": "Test Session",
            "summary": "Summary 2",
            "key_points": [],
        }],
        "overall_recap": "Daily recap",
    }

    report = validate_summary_output(duplicate_output, expected_identities)

    assert report.valid is False, "Must reject duplicate identity in full format"
    assert any("duplicate" in err.lower() for err in report.errors), (
        "Error must mention duplicate identity"
    )


# ====================================================================
# Section 4: _validated_item accepts strict content-only output for per-session
# ====================================================================


def test_validated_item_accepts_content_only_output():
    """_validated_item accepts strict content-only output (summary + key_points only)."""
    canonical = _daily("test-1", "Test Title")

    # Strict content-only output (new per-session contract)
    result = AuxiliaryResult(
        raw_json={
            "summary": "Valid summary content",
            "key_points": ["point 1", "point 2"],
        },
        session_summaries=[],
        overall_recap="",
    )

    summary, points = _validated_item(result, canonical, "test")

    assert summary == "Valid summary content"
    assert points == ["point 1", "point 2"]


def test_validated_item_rejects_full_format_for_per_session():
    """_validated_item REJECTS full format with identity fields for per-session."""
    canonical = _daily("test-1", "Test Title")

    # Full format with identity fields (old schema - WRONG for per-session)
    result = AuxiliaryResult(
        raw_json={
            "session_summaries": [{
                "profile": "default",
                "session_id": "test-1",
                "title": "Test Title",
                "summary": "Summary content",
                "key_points": ["point"],
            }],
            "overall_recap": "Daily recap",
        },
        session_summaries=[],
        overall_recap="",
    )

    # Must RAISE ValueError - full format is rejected for per-session
    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for full format output"
    except ValueError as exc:
        assert "session_summaries" in str(exc) or "invalid keys" in str(exc).lower(), (
            f"Error must reject session_summaries: {exc}"
        )


def test_validated_item_rejects_extra_keys():
    """_validated_item rejects content-only output with extra keys."""
    canonical = _daily("test-1", "Test Title")

    # Content-like but with extra keys
    result = AuxiliaryResult(
        raw_json={
            "summary": "Valid",
            "key_points": [],
            "profile": "default",  # NOT ALLOWED
            "session_id": "test-1",  # NOT ALLOWED
        },
        session_summaries=[],
        overall_recap="",
    )

    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for extra keys"
    except ValueError as exc:
        assert "invalid keys" in str(exc).lower(), f"Error must mention invalid keys: {exc}"


def test_validated_item_rejects_empty_summary():
    """_validated_item rejects content-only output with empty summary."""
    canonical = _daily("test-1", "Test Title")

    result = AuxiliaryResult(
        raw_json={
            "summary": "",  # Empty
            "key_points": [],
        },
        session_summaries=[],
        overall_recap="",
    )

    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for empty summary"
    except ValueError as exc:
        assert "summary" in str(exc).lower(), f"Error must mention summary: {exc}"


def test_validated_item_rejects_non_list_key_points():
    """_validated_item rejects content-only output with non-list key_points."""
    canonical = _daily("test-1", "Test Title")

    result = AuxiliaryResult(
        raw_json={
            "summary": "Valid",
            "key_points": "not a list",  # Must be list
        },
        session_summaries=[],
        overall_recap="",
    )

    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for non-list key_points"
    except ValueError as exc:
        assert "key_points" in str(exc).lower(), f"Error must mention key_points: {exc}"


def test_validated_item_rejects_overall_recap_key():
    """_validated_item rejects content-only output with overall_recap key."""
    canonical = _daily("test-1", "Test Title")

    result = AuxiliaryResult(
        raw_json={
            "summary": "Valid",
            "key_points": [],
            "overall_recap": "Not allowed",  # NOT ALLOWED
        },
        session_summaries=[],
        overall_recap="",
    )

    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for overall_recap key"
    except ValueError as exc:
        assert "invalid keys" in str(exc).lower() or "overall_recap" in str(exc), (
            f"Error must reject overall_recap: {exc}"
        )


def test_validated_item_rejects_session_summaries_key():
    """_validated_item rejects content-only output with session_summaries key."""
    canonical = _daily("test-1", "Test Title")

    result = AuxiliaryResult(
        raw_json={
            "summary": "Valid",
            "key_points": [],
            "session_summaries": [{"summary": "x"}],  # NOT ALLOWED
        },
        session_summaries=[],
        overall_recap="",
    )

    try:
        summary, points = _validated_item(result, canonical, "test")
        assert False, "Expected ValueError for session_summaries key"
    except ValueError as exc:
        assert "invalid keys" in str(exc).lower() or "session_summaries" in str(exc), (
            f"Error must reject session_summaries: {exc}"
        )
