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
