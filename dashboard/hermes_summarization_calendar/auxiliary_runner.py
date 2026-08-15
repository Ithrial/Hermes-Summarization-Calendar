"""Generic runner using Hermes' auxiliary.compression routing.

This module provides a stateless summary-model runner that delegates to
Hermes' configured auxiliary compression provider/model via the
agent.auxiliary_client.call_llm(task="compression", ...) seam.

The plugin uses the auxiliary routing policy configured in ~/.hermes/config.yaml
under auxiliary.compression.provider / model / base_url / timeout / reasoning_effort.
No hard-coded profile, model, or provider is used.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .limits import MAX_MODEL_PROMPT_BYTES

logger = logging.getLogger(__name__)

# Unique markers that the compression model must wrap JSON output in.
_MARKER_BEGIN = "LEDGER_JSON_BEGIN"
_MARKER_END = "LEDGER_JSON_END"


@dataclass
class AuxiliaryResult:
    """Parsed output from an auxiliary compression invocation."""

    session_summaries: list[dict] = field(default_factory=list)
    overall_recap: str = ""
    cron_summary: str = ""
    raw_json: dict = field(default_factory=dict)
    error: str | None = None
    # Metadata from the actual response
    response_model: str | None = None


def run_auxiliary_compression(
    prompt: str,
    *,
    ledger_root: Path | None = None,
) -> AuxiliaryResult:
    """Invoke the configured auxiliary compression provider via Hermes.

    Parameters
    ----------
    prompt :
        The full prompt text (already built by chunker).
    ledger_root :
        Optional ledger root for any stateful operations (not used by this runner).

    Returns
    -------
    AuxiliaryResult with parsed output or an error string.
    The result includes metadata (response_model) from the actual response when available.

    Raises
    ------
    RuntimeError
        If the Hermes auxiliary client cannot be loaded.
    """
    # Validate prompt size against conservative 48 KiB bound
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_MODEL_PROMPT_BYTES:
        logger.error(f"Prompt too large: {prompt_bytes} bytes")
        return AuxiliaryResult(
            error=f"Prompt exceeds size limit ({prompt_bytes} > {MAX_MODEL_PROMPT_BYTES} bytes)"
        )

    # Use the real Hermes core seam: agent.auxiliary_client.call_llm
    # This respects the config.yaml auxiliary.compression settings
    # without any provider/model/base_url/timeout overrides.
    try:
        from agent.auxiliary_client import call_llm
    except ImportError:
        logger.error("agent.auxiliary_client module not found")
        return AuxiliaryResult(
            error="Hermes agent.auxiliary_client not available. "
            "Ensure the running Hermes version supports call_llm."
        )

    # Build messages list for the compression task
    # The compression task expects a user message with the summary prompt
    messages = [
        {"role": "user", "content": prompt},
    ]

    # Build extra_body with response_format for generation-level structured output
    # This constrains the model to produce a pure JSON object without markdown wrappers
    extra_body = {"response_format": {"type": "json_object"}}

    try:
        response = call_llm(task="compression", messages=messages, extra_body=extra_body)
    except Exception as exc:
        sanitized = _sanitize_error(f"Compression task failed: {exc}")
        logger.error(sanitized)
        return AuxiliaryResult(error=sanitized)

    # Extract the raw text from the chat-completion-compatible response object
    try:
        content = _extract_message_content(response)
    except Exception as exc:
        sanitized = _sanitize_error(f"Failed to extract response content: {exc}")
        logger.error(sanitized)
        return AuxiliaryResult(error=sanitized)

    # Extract response model from object or dict, only accepting non-empty trimmed string
    response_model = None
    if hasattr(response, "model"):
        model_val = getattr(response, "model")
        if isinstance(model_val, str) and model_val.strip():
            response_model = model_val.strip()
    elif isinstance(response, dict) and "model" in response:
        model_val = response.get("model")
        if isinstance(model_val, str) and model_val.strip():
            response_model = model_val.strip()

    # Parse JSON from content: try bare JSON first, fall back to marker-wrapped
    return _parse_compression_output(content, response_model)


def _sanitize_error(message: str) -> str:
    """Sanitize error message to prevent credential/path leakage.

    Replaces filesystem paths and potential secrets with generic placeholders.
    Bounds the returned error length to prevent excessive token usage.
    """
    # Replace common path patterns - do NOT convert to ~ first
    sanitized = message
    import os
    home = os.environ.get("HOME", "")
    if home:
        sanitized = sanitized.replace(home, "/<redacted>")
    sanitized = sanitized.replace("/home/", "/<redacted>/")
    sanitized = sanitized.replace("/tmp/", "/<redacted>/")

    # Replace potential token patterns
    import re
    sanitized = re.sub(r"token[_:\s]*[a-zA-Z0-9_-]{8,}", "token_<redacted>", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"api[_:\s]*key[_:\s]*[a-zA-Z0-9_-]{8,}", "api_key_<redacted>", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"secret[_:\s]*[a-zA-Z0-9_-]{8,}", "secret_<redacted>", sanitized, flags=re.IGNORECASE)

    # Replace long opaque bearer-like values
    sanitized = re.sub(r"(Bearer|bearer)\s+[a-zA-Z0-9_-]{64,}", "Bearer <redacted>", sanitized)

    # Bound the length to prevent excessive token usage in logs
    max_len = 1024
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "..."
    return sanitized


def _extract_message_content(response: Any) -> str:
    """Extract message content from a chat-completion-compatible response.

    Supports:
    - response.choices[0] as object or dict
    - response.choices[0].message as object, dict, or string
    - response.choices[0].message.content as string, or dict with 'text'/'value' key

    Returns the text content or raises ValueError if extraction fails.
    Never includes raw content in error messages.
    """
    # Step 1: Get choices array (support object or dict)
    choices = None
    if hasattr(response, "choices"):
        choices = getattr(response, "choices")
    elif isinstance(response, dict) and "choices" in response:
        choices = response.get("choices")
    if not choices or not isinstance(choices, list) or len(choices) == 0:
        raise ValueError("Response missing valid choices array")

    # Step 2: Get first choice (support object or dict)
    choice = None
    if hasattr(choices[0], "message"):
        choice = choices[0]
    elif isinstance(choices[0], dict) and "message" in choices[0]:
        choice = choices[0]
    if choice is None:
        raise ValueError("Choice missing message")

    # Step 3: Get message (support object, dict, or string)
    message = None
    if hasattr(choice, "message"):
        message = getattr(choice, "message")
    elif isinstance(choice, dict):
        message = choice.get("message")
    else:
        raise ValueError("Choice missing message")

    # Step 4: Handle message being a string directly
    if isinstance(message, str):
        if not message.strip():
            raise ValueError("Message content is empty")
        return message

    # Step 5: Get message.content (support string or dict)
    content = None
    if hasattr(message, "content"):
        content = getattr(message, "content")
    elif isinstance(message, dict) and "content" in message:
        content = message.get("content")

    if content is None:
        raise ValueError("Message has no content")

    # Step 6: Handle string content
    if isinstance(content, str):
        if not content.strip():
            raise ValueError("Message content is empty")
        return content

    # Step 7: Handle dict content (structured-output style)
    if isinstance(content, dict):
        text = content.get("text") or content.get("value")
        if isinstance(text, str) and text.strip():
            return text
        raise ValueError("Message content dict has no valid text field")

    raise ValueError(f"Unsupported message content type: {type(content).__name__}")


def _scan_json_structure(json_str: str) -> list[str]:
    """Scan JSON string and return the stack of unclosed structural characters.

    This is quote/escape-aware and does NOT count braces/brackets inside strings.

    Returns a list like ["{"] (missing closing brace) or ["{","["] (missing ]
    before final }).

    The scan stops at the first parse error or at EOF with an incomplete structure.
    """
    stack: list[str] = []
    i = 0
    n = len(json_str)

    while i < n:
        ch = json_str[i]

        # Skip whitespace
        if ch in " \t\n\r":
            i += 1
            continue

        # Handle strings - skip entire string content
        if ch == '"':
            i += 1
            while i < n:
                c = json_str[i]
                if c == '\\' and i + 1 < n:
                    # Skip escaped character
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                i += 1
            continue

        # Handle structural characters - only JSON structural delimiters
        if ch in '{[':
            stack.append(ch)
            i += 1
        elif ch in ']}':
            if not stack:
                # Unexpected closing - the parse error is here
                return stack
            expected_open = {'}': '{', ']': '['}.get(ch)
            if stack[-1] != expected_open:
                # Mismatch - the parse error is here
                return stack
            stack.pop()
            i += 1
        else:
            # Skip other JSON characters (like : ,) and continue
            i += 1

    return stack


def _attempt_repair(json_str: str) -> tuple[bool, str | None]:
    """Attempt to repair a malformed JSON string by adding exactly one missing closer.

    Returns (success, repaired_string).

    Case A: Stack is exactly ["{"] - append "}" to close the top-level object
    Case B: Stack is exactly ["{","["] - insert "]" before the final non-whitespace "}"

    All other cases return failure.
    """
    stack = _scan_json_structure(json_str)

    # Case A: Missing final } - stack is ["{"]
    if stack == ["{"]:
        repaired = json_str + "}"
        return (True, repaired)

    # Case B: Missing ] before final } - stack is ["{","["]
    if stack == ["{", "["]:
        # Find the last non-whitespace } and insert ] before it
        # First, find position of last non-whitespace character
        stripped = json_str.rstrip()
        if stripped.endswith("}"):
            # Find position of the final } in the stripped string
            brace_pos = stripped.rfind("}")
            repaired = stripped[:brace_pos] + "]" + stripped[brace_pos:]
            # Preserve any trailing whitespace that was stripped
            repaired += json_str[len(stripped):]
            return (True, repaired)

    return (False, None)


def _parse_compression_output(
    raw: str, response_model: str | None = None
) -> AuxiliaryResult:
    """Extract and validate JSON.

    First tries to parse as bare JSON (no markers). If that fails or isn't found,
    falls back to marker-wrapped parsing (LEDGER_JSON_BEGIN/LEDGER_JSON_END)
    for backward compatibility with legacy outputs.

    Malformed JSON fails closed unless it matches the exact repairable patterns.
    """
    raw = raw.strip()

    # Phase 1: Try bare JSON first (generation is constrained via response_format)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # Bare JSON succeeded - validate schema
            return _validate_and_build_result(data, raw, response_model)
        else:
            # Bare JSON parsed but is not a dict - fail closed
            return AuxiliaryResult(
                error="Compression output must be a JSON object, not list or scalar"
            )
    except json.JSONDecodeError:
        # Not valid bare JSON, try repair
        success, repaired = _attempt_repair(raw)
        if success:
            try:
                data = json.loads(repaired)
                if isinstance(data, dict):
                    logger.warning("Restored one missing terminal JSON structural closer in compression output")
                    return _validate_and_build_result(data, repaired, response_model)
            except json.JSONDecodeError:
                pass

        # Not repairable, continue to marker-wrapped parsing
        pass

    # Phase 2: Fall back to marker-wrapped parsing for legacy outputs
    pattern = re.escape(_MARKER_BEGIN) + r"\s*(.*?)\s*" + re.escape(_MARKER_END)
    matches = re.findall(pattern, raw, re.DOTALL)

    if not matches:
        logger.warning("No LEDGER_JSON markers found in compression output")
        return AuxiliaryResult(
            error="Invalid JSON from compression"
        )

    # Reject multiple marker pairs (contract requires exactly one)
    if len(matches) > 1:
        return AuxiliaryResult(
            error="Multiple LEDGER_JSON marker pairs found (expected exactly one)"
        )

    # Use the first (and should be unique) match
    json_str = matches[0].strip()
    source_str = json_str  # Track the original string for validation

    # Try to parse with repair
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Try repair for marker-wrapped JSON as well
        success, repaired = _attempt_repair(json_str)
        if success:
            try:
                data = json.loads(repaired)
                if isinstance(data, dict):
                    logger.warning("Restored one missing terminal JSON structural closer in compression output")
                    source_str = repaired
                else:
                    data = None
            except json.JSONDecodeError:
                data = None
        else:
            data = None

        if data is None:
            logger.error("Invalid JSON in marker-wrapped output - generation must be constrained")
            return AuxiliaryResult(error="Invalid JSON from compression")

    # Require data is a dict before key checks
    if not isinstance(data, dict):
        return AuxiliaryResult(
            error="Compression output must be a JSON object, not list or scalar"
        )

    # Validate and build result
    return _validate_and_build_result(data, source_str, response_model)


def _validate_and_build_result(
    data: dict, json_str: str, response_model: str | None
) -> AuxiliaryResult:
    """Validate parsed data and build AuxiliaryResult.

    Rejects legacy top-level keys that belong to obsolete schema.
    """
    # Reject legacy top-level keys that belong to obsolete schema
    if "date" in data:
        return AuxiliaryResult(
            error="Legacy 'date' key found in output (obsolete schema)"
        )
    if "sessions" in data:
        return AuxiliaryResult(
            error="Legacy 'sessions' key found in output (obsolete schema)"
        )

    result = AuxiliaryResult(
        session_summaries=data.get("session_summaries", []),
        overall_recap=data.get("overall_recap", ""),
        cron_summary=data.get("cron_summary", ""),
        raw_json=data,
        response_model=response_model,
    )

    return result
