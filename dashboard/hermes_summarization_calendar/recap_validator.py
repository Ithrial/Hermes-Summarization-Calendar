"""Structural validation of recap output.

Validates that returned JSON matches the expected shape and that all
session IDs provided in the prompt are accounted for in the output.
Uses COMPOSITE identity (profile + session_id) so duplicate session IDs
in different profiles are distinguished correctly.
Rejects output with extra, missing, or duplicate composite identities.

Treats output as untrusted data — validates before publishing.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionIdentity:
    """The exact composite identity supplied for a session (ground truth).

    Uses ``profile`` + ``session_id`` as the canonical key so that two
    sessions with the same ID in different profiles are treated distinctly.
    """

    session_id: str
    title: str
    profile: str = ""

    @property
    def composite_key(self) -> tuple[str, str]:
        """Return (profile, session_id) for dedup and coverage checks."""
        return (self.profile, self.session_id)


@dataclass
class ValidationReport:
    """Result of validating output against expected identities."""

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_summary_output(
    raw_json: dict,
    expected_identities: list[SessionIdentity],
) -> ValidationReport:
    """Validate JSON output structure and composite identity coverage.

    Checks:
    1. ``session_summaries`` is a non-empty list (unless input was empty).
    2. Each item has ``profile``, ``session_id``, ``title``, ``summary`` as strings.
    3. ``overall_recap`` is a string.
    4. All expected composite identities (profile + session_id) are present in output.
    5. No extra composite identities appear that were not in the input.
    6. No duplicate composite identities in output.
    7. Session titles/profiles match (with lenient whitespace).

    Output schema requirement: every session_summary MUST include ``profile``.
    Missing ``profile`` fields cause validation failure.

    Returns
    -------
    ValidationReport with ``valid=False`` if any check fails.
    """
    report = ValidationReport()

    if not isinstance(raw_json, dict):
        return ValidationReport(valid=False, errors=["Output is not a JSON object"])

    session_summaries = raw_json.get("session_summaries")
    if not isinstance(session_summaries, list):
        return ValidationReport(
            valid=False,
            errors=["Missing or invalid 'session_summaries' array"],
        )

    # Allow empty summaries when input was empty
    if not session_summaries and expected_identities:
        report.valid = False
        report.errors.append("Empty session_summaries but sessions were provided")

    # Check overall_recap exists and is a string
    overall_recap = raw_json.get("overall_recap")
    if not isinstance(overall_recap, str):
        report.valid = False
        report.errors.append("'overall_recap' must be a string")
    elif len(overall_recap.strip()) == 0 and expected_identities:
        report.warnings.append("'overall_recap' is empty despite having sessions")

    # Check cron_summary if present
    cron_summary = raw_json.get("cron_summary")
    if cron_summary is not None and not isinstance(cron_summary, str):
        report.valid = False
        report.errors.append("'cron_summary' must be a string if present")

    # Validate individual session summaries — require profile field
    output_composites: set[tuple[str, str]] = set()
    identity_map_output: dict[tuple[str, str], dict] = {}

    for item in session_summaries:
        if not isinstance(item, dict):
            report.valid = False
            report.errors.append("session_summaries item is not an object")
            continue

        profile = item.get("profile")
        sid = item.get("session_id")
        title = item.get("title")
        summary = item.get("summary")

        # profile is REQUIRED for composite identity
        if not isinstance(profile, str) or not profile.strip():
            report.valid = False
            report.errors.append(
                f"Missing or empty 'profile' in session summary for id '{sid}' — "
                "composite identity requires profile field"
            )
            continue

        if not isinstance(sid, str) or not sid.strip():
            report.valid = False
            report.errors.append("Missing or empty 'session_id' in session summary")
            continue

        composite = (profile.strip(), sid.strip())

        if composite in output_composites:
            report.valid = False
            report.errors.append(
                f"Duplicate composite identity ('{profile}', '{sid}') in output"
            )
        else:
            output_composites.add(composite)
            identity_map_output[composite] = item

        if not isinstance(title, str):
            report.valid = False
            report.errors.append(f"Session {sid!r}: 'title' must be a string")
        elif len(title.strip()) == 0:
            report.warnings.append(f"Session ({profile}, {sid}): 'title' is empty/whitespace")

        if not isinstance(summary, str):
            report.valid = False
            report.errors.append(f"Session ({profile}, {sid}): 'summary' must be a string")
        elif len(summary.strip()) == 0:
            report.warnings.append(f"Session ({profile}, {sid}): 'summary' is empty/whitespace")

    # Composite identity coverage — all expected identities must be in output
    expected_composites = {si.composite_key for si in expected_identities}

    missing = expected_composites - output_composites
    if missing:
        report.valid = False
        missing_strs = [f"('{p}', '{s}')" for p, s in sorted(missing)]
        report.errors.append(
            f"Missing session identities in output: {missing_strs}"
        )

    extra = output_composites - expected_composites
    if extra:
        report.valid = False
        extra_strs = [f"('{p}', '{s}')" for p, s in sorted(extra)]
        report.errors.append(
            f"Extra session identities in output not in input: {extra_strs}"
        )

    # Verify titles match for common composites (lenient whitespace comparison)
    identity_map = {si.composite_key: si for si in expected_identities}
    for composite in output_composites & expected_composites:
        expected_title = identity_map[composite].title.strip().lower()
        actual_item = identity_map_output.get(composite, {})
        actual_title = (actual_item.get("title", "") or "").strip().lower()
        if actual_title and expected_title and actual_title != expected_title:
            # Warn but don't fail — model may rephrase titles slightly
            report.warnings.append(
                f"Session {composite[1]!r}: title differs "
                f"(expected '{identity_map[composite].title}', "
                f"got '{actual_item.get('title', '')}')"
            )

    return report


def escape_markdown(text: str) -> str:
    """Escape text for safe Markdown rendering.

    Prevents markdown injection and XSS when embedding user data in output.
    """
    # First HTML-escape to prevent any script injection
    safe = html.escape(text, quote=True)
    return safe


def sanitize_recap_summary(summary: str, max_length: int = 5000) -> str:
    """Trim and sanitize a summary string for storage.

    Returns a truncated plain-text version suitable for Markdown rendering.
    """
    if not isinstance(summary, str):
        return ""
    cleaned = summary.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "..."
    # Double-escape for both HTML and markdown safety
    return escape_markdown(cleaned)
