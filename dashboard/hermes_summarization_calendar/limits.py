"""Shared constants for prompt size limits.

This module provides central definitions for byte-based limits that must be
consistent across chunker, runner, roll-up, and validation logic.

The 48 KiB (49,152 bytes) limit is derived from Hermes core constraints:
- MINIMUM_CONTEXT_LENGTH = 64,000 tokens in agent/model_metadata.py
- Worst-case UTF-8 byte-to-token expansion is 1:1 (no expansion)
- 48 KiB = 49,152 bytes <= 49,152 tokens worst case
- Leaves 14,848 tokens for output and overhead (64,000 - 49,152 = 14,848)

The runner's prompt_size > MAX_MODEL_PROMPT_BYTES check remains the fail-closed
boundary. All callers must respect this limit; exceeding it at any layer
results in explicit failure before reaching the runner.

No config keys are introduced. The 48 KiB bound is a safe conservative
constant shared by all orchestration paths.
"""

from __future__ import annotations

# Maximum prompt size in bytes for model calls.
# This is a conservative limit based on Hermes' auxiliary compression constraints.
# 48 KiB = 49,152 bytes ensures we stay well below the 64,000-token minimum context.
MAX_MODEL_PROMPT_BYTES = 48 * 1024  # 49,152 bytes = 48 KiB

# ---------------------------------------------------------------------------
# Cumulative per-job budgets (v1.2.4 security scan finding 1, CWE-770).
#
# The per-prompt ceiling above bounds a single model call, but nothing bounded
# the aggregate work a single user-triggered job could do: total transcript
# bytes, chunk count, or provider calls. A Dashboard caller who selected a very
# large stored session could make the plugin issue one provider request per
# chunk plus reduction work, consuming provider quota and worker capacity
# without limit.
#
# These constants bound the *total* work for one job. They are checked in the
# orchestration layer BEFORE the first provider call (fail fast) and again
# around each call (fail closed if a budget is exhausted mid-job). They are
# deliberately separate from MAX_MODEL_PROMPT_BYTES, which bounds a single
# prompt argument.
# ---------------------------------------------------------------------------

# Maximum total raw transcript bytes eligible for one summary job.
# Beyond this a session is too large to summarize in one job; the job is
# rejected before any provider call with a stable, retryable error.
# 2 MiB is comfortably above any realistic single-day session while bounding
# worst-case memory and provider-call count.
MAX_SESSION_SOURCE_BYTES = 2 * 1024 * 1024  # 2,097,152 bytes = 2 MiB

# Maximum number of provider calls a single summary job may issue (chunks plus
# every reduction pass). Bounds cost and wall-clock regardless of how the
# source is chunked.
#
# Consistency note: at the worst case of one chunk per ~45 KiB of content,
# MAX_SESSION_SOURCE_BYTES can produce at most ~46 chunks, so the byte budget
# always fires first for oversized sessions. The call budget is the backstop
# for pathological chunking and for the extra calls made by hierarchical
# reduction.
MAX_SESSION_PROVIDER_CALLS = 64
