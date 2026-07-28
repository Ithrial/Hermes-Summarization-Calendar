"""Regression tests for identity-safe recap orchestration."""
from __future__ import annotations
from types import SimpleNamespace

from hermes_daily_ledger.auxiliary_runner import AuxiliaryResult
from hermes_daily_ledger.contract import DailySession
from hermes_daily_ledger.recap_orchestrator import (
    _merge_chunk_session_summaries,
    _reconcile_inventory_transcripts,
    _validate_chunk_result,
)
from hermes_daily_ledger.recap_validator import SessionIdentity
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


def _aux_result(*summaries: dict, overall: str = "chunk") -> AuxiliaryResult:
    return AuxiliaryResult(session_summaries=list(summaries), overall_recap=overall)


def test_inventory_only_session_is_added_as_metadata_only_transcript():
    existing = _transcript("s1", "Session One")
    reconciled = _reconcile_inventory_transcripts(
        [existing],
        [_daily("s1", "Session One"), _daily("s2", "System-only Session")],
    )

    assert [(t.profile, t.session_id) for t in reconciled] == [
        ("default", "s1"),
        ("default", "s2"),
    ]
    assert reconciled[0] is existing
    assert reconciled[1].title == "System-only Session"
    assert reconciled[1].messages == []


def test_chunk_result_rejects_missing_and_extra_composite_identities():
    chunk = SimpleNamespace(session_transcripts=[{
        "profile": "default",
        "session_id": "required",
        "title": "Required",
    }])
    result = _aux_result({
        "profile": "default",
        "session_id": "invented",
        "title": "Invented",
        "summary": "not in chunk",
    })

    report = _validate_chunk_result(result, chunk)

    assert report.valid is False
    assert any("Missing session identities" in error for error in report.errors)
    assert any("Extra session identities" in error for error in report.errors)


def test_merge_preserves_canonical_membership_and_all_split_session_parts():
    expected = [
        SessionIdentity("s1", "Actual One", "default"),
        SessionIdentity("s2", "Actual Two", "default"),
    ]
    chunk_results = [
        _aux_result({
            "profile": "default",
            "session_id": "s1",
            "title": "Rephrased One",
            "summary": "first segment",
        }),
        _aux_result({
            "profile": "default",
            "session_id": "s1",
            "title": "Another Rephrase",
            "summary": "second segment",
        }),
        _aux_result({
            "profile": "default",
            "session_id": "s2",
            "title": "Rephrased Two",
            "summary": "only segment",
        }),
    ]

    merged = _merge_chunk_session_summaries(chunk_results, expected)

    assert [(item["profile"], item["session_id"]) for item in merged] == [
        ("default", "s1"),
        ("default", "s2"),
    ]
    assert [item["title"] for item in merged] == ["Actual One", "Actual Two"]
    assert "first segment" in merged[0]["summary"]
    assert "second segment" in merged[0]["summary"]
    assert merged[1]["summary"] == "only segment"
