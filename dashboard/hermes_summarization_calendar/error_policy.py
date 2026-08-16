"""Shared redaction for error text crossing public boundaries.

The v1.2.4 security scan (finding 6) documented duplicated, inconsistent
sanitizer denylists and raw exception text reaching API-visible responses.
This module is the single boundary redactor for error text that becomes
visible to API callers:

- error fields persisted to durable job state (returned by status endpoints)
- any other diagnostic string about to cross the HTTP response boundary

Rules enforced at the API layer (see ``plugin_api.py``):
- Raw exception text is NEVER interpolated into HTTP responses. Routes
  return fixed, pre-approved message strings and log full diagnostics
  server-side via ``logger.exception`` instead.
- Error text stored in job state is passed through :func:`redact_error`
  before persistence.

Internal sanitizers that are NOT public boundaries are intentionally not
converged here:

- ``inventory._sanitize_error`` feeds the deterministic cron fingerprint
  digest; changing its behavior would change fingerprints and mark every
  existing summary stale.
- ``batch_orchestrator``, ``batch_jobs``, and ``auxiliary_runner`` carry
  pinned behavioral tests with their own placeholder contracts and serve
  storage/log surfaces.
"""

from __future__ import annotations

import re

# Maximum length of persisted, API-visible error text.
MAX_PERSISTED_ERROR_CHARS = 500

# Redaction patterns, applied in order (most specific first).
# Covers the shapes the scan named as missing: macOS /Users and /private
# paths, credential-bearing URLs, bearer tokens, and non-hex secret formats.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization header values and bearer tokens
    (
        re.compile(
            r"\b(?:bearer|authorization)\b\s*[:=]?\s*[A-Za-z0-9._\-+/=]{8,}",
            re.IGNORECASE,
        ),
        "Authorization: [REDACTED]",
    ),
    # Keyed credentials: token=..., api_key: "..." (quoted or bare values)
    (
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|token|secret|password|passwd|private[_-]?key|auth)"
            r"\b\s*[:=]\s*['\"]?([^\s'\",;}{]{6,})",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
    # token_xxx / api_key_xxx style identifiers (underscore joined secrets)
    (
        re.compile(
            r"\b(token|api_?key|secret|password|passwd|auth)[_-][A-Za-z0-9_\-]{8,}",
            re.IGNORECASE,
        ),
        r"\1_[REDACTED]",
    ),
    # Credential-bearing URLs: scheme://user:pass@host
    (
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*)://([^/@\s]+)@", re.IGNORECASE),
        r"\1://[REDACTED]@",
    ),
    # Long hex strings (API keys, digests, bearer values)
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
    # Long opaque high-entropy strings (base64-ish secrets)
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{48,}={0,2}\b"), "[REDACTED]"),
    # Process IDs
    (re.compile(r"\bpid[_\s]?=?\s*\d+", re.IGNORECASE), "[PID]"),
    # Absolute Unix paths, including macOS /Users and /private
    (
        re.compile(
            r"/(?:home|Users|private|var|tmp|opt|mnt|srv|data|root|usr|etc)/[\w./\-]*"
        ),
        "[PATH]",
    ),
]


def redact_error(raw: str, max_chars: int = MAX_PERSISTED_ERROR_CHARS) -> str:
    """Redact path, credential, and process metadata from error text.

    Bounds the result to ``max_chars``. Returns an empty string for
    non-string or empty input. This is the boundary redactor for any
    error text that may be returned by a status endpoint.
    """
    if not raw or not isinstance(raw, str):
        return ""
    sanitized = raw.strip()[:max_chars]
    for pattern, replacement in _REDACT_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized
