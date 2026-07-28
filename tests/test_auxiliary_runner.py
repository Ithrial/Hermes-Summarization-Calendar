"""Tests for the auxiliary compression runner seam.

These tests verify that the plugin uses Hermes' auxiliary.compression
routing without hard-coding a profile or model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

# Import after path setup
from hermes_daily_ledger.auxiliary_runner import (
    AuxiliaryResult,
    run_auxiliary_compression,
    _parse_compression_output,
)
from hermes_daily_ledger.inventory import discover_all
from hermes_daily_ledger.session_orchestrator import generate_session_summary
from hermes_daily_ledger.session_storage import save_session_summary
from hermes_daily_ledger.summary_jobs import _reset_for_tests
from hermes_daily_ledger.limits import MAX_MODEL_PROMPT_BYTES

DATE = "2026-03-08"
PROFILE = "default"
SESSION_ID = "20260308_100000_bbb"
TITLE = "DST migration task"


@pytest.fixture(autouse=True)
def reset_jobs():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _make_session_summary_data():
    return {
        "session_summaries": [{
            "profile": PROFILE,
            "session_id": SESSION_ID,
            "title": TITLE,
            "summary": "Test summary",
            "key_points": ["Test point"],
        }],
        "overall_recap": "Test summary",
        "cron_summary": "",
    }


def _wrap_with_markers(data: dict) -> str:
    """Wrap JSON in LEDGER_JSON markers as the compression model would."""
    return (
        f"LEDGER_JSON_BEGIN\n"
        f"{json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)}"
        f"\nLEDGER_JSON_END"
    )


def _create_chat_response(content: str, model: str = "test-model") -> SimpleNamespace:
    """Create a mock chat-completion-compatible response object."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    response = SimpleNamespace(
        choices=[choice],
        model=model,
        id="test-id",
    )
    return response


class TestRunAuxiliaryCompression:
    def test_runs_compression_task_with_prompt(self, tmp_path: Path):
        """Verify the compression task is called with the correct task name and prompt."""
        captured_task = None
        captured_messages = None

        def mock_call_llm(task: str, messages: list[dict]) -> SimpleNamespace:
            nonlocal captured_task, captured_messages
            captured_task = task
            captured_messages = messages
            data = _make_session_summary_data()
            content = _wrap_with_markers(data)
            return _create_chat_response(content, model="test-model")

        mock_client = MagicMock()
        mock_client.chat = MagicMock()
        mock_client.chat.completions = MagicMock()
        mock_client.chat.completions.create = lambda **kw: mock_call_llm(kw.get("task"), kw.get("messages"))

        with patch("agent.auxiliary_client.call_llm", mock_client.chat.completions.create):
            result = run_auxiliary_compression(
                prompt="Test prompt content",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert captured_task == "compression"
        assert len(captured_messages) == 1
        assert captured_messages[0]["role"] == "user"
        assert "Test prompt content" in captured_messages[0]["content"]

    def test_fails_when_compression_returns_error(self, tmp_path: Path):
        """Verify that compression task failures produce an error result."""
        with patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("Model unavailable")):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Model unavailable" in result.error

    def test_fails_when_output_missing_markers(self, tmp_path: Path):
        """Verify missing markers from compression task is treated as failure."""
        def mock_call_llm(**kw) -> SimpleNamespace:
            message = SimpleNamespace(content="No markers here")
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice], model="test")

        with patch("agent.auxiliary_client.call_llm", mock_call_llm):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_when_output_invalid_json(self, tmp_path: Path):
        """Verify invalid JSON from compression task is treated as failure."""
        def mock_call_llm(**kw) -> SimpleNamespace:
            message = SimpleNamespace(content="LEDGER_JSON_BEGIN\nnot valid json\nLEDGER_JSON_END")
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice], model="test")

        with patch("agent.auxiliary_client.call_llm", mock_call_llm):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_repairs_one_missing_final_top_level_brace(self, tmp_path: Path):
        """Accept observed terminal-closer omission: one final object brace omitted at EOF."""
        content = (
            'LEDGER_JSON_BEGIN\n'
            '{"summary":"Valid content","key_points":["One point"]\n'
            'LEDGER_JSON_END'
        )
        response = _create_chat_response(content, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json == {
            "summary": "Valid content",
            "key_points": ["One point"],
        }

    def test_repairs_one_missing_final_key_points_bracket(self, tmp_path: Path):
        """Accept observed terminal-closer omission: final key_points bracket omitted before `}`."""
        content = (
            'LEDGER_JSON_BEGIN\n'
            '{"summary":"Valid content","key_points":["One point"]}\n'
            'LEDGER_JSON_END'
        )
        # Remove the ] before the final } -> becomes {"key_points":["One point"}
        content = content.replace('"One point"]', '"One point"')
        response = _create_chat_response(content, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json == {
            "summary": "Valid content",
            "key_points": ["One point"],
        }

    def test_repairs_bare_missing_final_brace(self, tmp_path: Path):
        """Bare JSON missing final `}` is repaired."""
        bare_json = '{"summary":"Valid content","key_points":["One point"]'
        response = _create_chat_response(bare_json, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json == {
            "summary": "Valid content",
            "key_points": ["One point"],
        }

    def test_repairs_bare_missing_key_points_bracket(self, tmp_path: Path):
        """Bare JSON missing `]` before final `}` is repaired."""
        # Start with valid JSON: {"summary":"test","key_points":["One point"]}
        # Remove the ] before the final } -> becomes {"summary":"test","key_points":["One point"}
        bare_json = '{"summary":"test","key_points":["One point"]}'
        # Remove the ] from ["One point"] -> becomes ["One point"
        bare_json = bare_json.replace('"One point"]', '"One point"')
        response = _create_chat_response(bare_json, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json == {
            "summary": "test",
            "key_points": ["One point"],
        }

    def test_records_response_model(self, tmp_path: Path):
        """Verify the actual response model is recorded in the result."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)
        response = _create_chat_response(content, model="fixture-provider/fixture-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.response_model == "fixture-provider/fixture-model"

    def test_fails_when_response_shape_invalid(self, tmp_path: Path):
        """Verify malformed response from compression task is treated as failure."""
        def mock_call_llm(**kw):
            # Response without choices
            return SimpleNamespace(model="test")

        with patch("agent.auxiliary_client.call_llm", mock_call_llm):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        # Updated to match actual error message format
        assert "response missing valid choices" in result.error.lower() or "choices" in result.error.lower()

    def test_fails_when_prompt_exceeds_limit(self, tmp_path: Path):
        """Verify prompts exceeding MAX_MODEL_PROMPT_BYTES are rejected."""
        large_prompt = "x" * (MAX_MODEL_PROMPT_BYTES + 1)

        result = run_auxiliary_compression(
            prompt=large_prompt,
            ledger_root=tmp_path / "ledger",
        )

        assert result.error is not None
        assert "Prompt exceeds size limit" in result.error


class TestAuxiliaryCompressionExactLimit:
    """Tests for exact boundary conditions on the 48 KiB limit."""

    def test_prompt_exactly_at_limit_reaches_call_llm(self, tmp_path: Path):
        """A prompt with byte length exactly MAX_MODEL_PROMPT_BYTES reaches mocked call_llm and parses successfully."""
        exact_bytes = MAX_MODEL_PROMPT_BYTES
        prompt = "x" * exact_bytes
        assert len(prompt.encode("utf-8")) == exact_bytes

        def mock_call_llm(task: str, messages: list[dict], extra_body: dict | None = None) -> SimpleNamespace:
            data = _make_session_summary_data()
            content = _wrap_with_markers(data)
            return _create_chat_response(content, model="test-model")

        call_llm_called = [False]
        def counting_mock(*args, **kwargs):
            call_llm_called[0] = True
            return mock_call_llm(*args, **kwargs)

        with patch("agent.auxiliary_client.call_llm", counting_mock):
            result = run_auxiliary_compression(
                prompt=prompt,
                ledger_root=tmp_path / "ledger",
            )

        # Should reach call_llm (not rejected at validation)
        assert call_llm_called[0], "call_llm should have been called for exact-limit prompt"
        assert result.error is None, f"Should parse successfully, got: {result.error}"

    def test_prompt_one_over_limit_fails_without_call_llm(self, tmp_path: Path):
        """A prompt with byte length MAX_MODEL_PROMPT_BYTES + 1 fails without invoking call_llm."""
        over_bytes = MAX_MODEL_PROMPT_BYTES + 1
        prompt = "x" * over_bytes
        assert len(prompt.encode("utf-8")) == over_bytes

        call_llm_called = [False]
        def tracking_mock(*args, **kwargs):
            call_llm_called[0] = True
            return _create_chat_response("LEDGER_JSON_BEGIN\n{}\nLEDGER_JSON_END")

        with patch("agent.auxiliary_client.call_llm", tracking_mock):
            result = run_auxiliary_compression(
                prompt=prompt,
                ledger_root=tmp_path / "ledger",
            )

        # Should NOT reach call_llm (rejected at validation)
        assert not call_llm_called[0], "call_llm should NOT have been called for over-limit prompt"
        assert result.error is not None
        assert "Prompt exceeds size limit" in result.error


class TestResponseModelHandling:
    """Tests for response model extraction from object or dict shaped responses."""

    def test_dict_response_with_valid_model_carries_model(self, tmp_path: Path):
        """Dict-shaped response carries a non-empty trimmed model through to AuxiliaryResult.response_model."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)

        # Mock response as a dict (like what auxiliary_client returns)
        response_dict = {
            "choices": [{"message": {"content": content}}],
            "model": "fixture-provider/fixture-model",
            "id": "test-id",
        }

        with patch("agent.auxiliary_client.call_llm", return_value=response_dict):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.response_model == "fixture-provider/fixture-model"

    def test_dict_response_with_blank_model_records_none(self, tmp_path: Path):
        """Dict-shaped response with blank/non-string model records None."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)

        response_dict = {
            "choices": [{"message": {"content": content}}],
            "model": "",  # Blank string
        }

        with patch("agent.auxiliary_client.call_llm", return_value=response_dict):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.response_model is None

    def test_dict_response_with_null_model_records_none(self, tmp_path: Path):
        """Dict-shaped response with null/None model records None."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)

        response_dict = {
            "choices": [{"message": {"content": content}}],
            "model": None,
        }

        with patch("agent.auxiliary_client.call_llm", return_value=response_dict):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.response_model is None

    def test_object_response_with_whitespace_trimmed_model(self, tmp_path: Path):
        """Response with whitespace-only model string is trimmed and stored if non-empty."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)
        # Object with whitespace that trims to valid model
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            model="  test-model  ",  # Whitespace around
            id="test-id",
        )

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        # Model should be trimmed
        assert result.response_model == "test-model"


class TestCompressionOutputValidation:
    """Tests for _parse_compression_output rejecting non-object JSON."""

    def test_list_json_rejected(self):
        """List JSON between markers returns a clean non-object error and never raises."""
        list_json = json.dumps([{"session_id": "s1", "title": "T1", "summary": "S1"}])
        raw = f"LEDGER_JSON_BEGIN\n{list_json}\nLEDGER_JSON_END"

        result = _parse_compression_output(raw)

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()
        assert result.session_summaries == []
        assert result.overall_recap == ""

    def test_scalar_string_rejected(self):
        """Scalar string JSON between markers returns a clean non-object error."""
        raw = "LEDGER_JSON_BEGIN\n\"just a string\"\nLEDGER_JSON_END"

        result = _parse_compression_output(raw)

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()

    def test_scalar_number_rejected(self):
        """Scalar number JSON between markers returns a clean non-object error."""
        raw = "LEDGER_JSON_BEGIN\n42\nLEDGER_JSON_END"

        result = _parse_compression_output(raw)

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()

    def test_null_rejected(self):
        """Null JSON between markers returns a clean non-object error."""
        raw = "LEDGER_JSON_BEGIN\nnull\nLEDGER_JSON_END"

        result = _parse_compression_output(raw)

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()


class TestMalformedJSONRejection:
    """Tests for truly non-repairable malformed JSON."""

    def test_fails_with_unclosed_array(self, tmp_path: Path):
        """Unclosed array (missing both ] and }) is not repairable."""
        # The array is still open (has [ but no ]) AND the object is open (has { but no })
        # This is two missing closers and cannot be repaired by appending or inserting one character
        malformed = '{"summary":"test","items":['
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_open_string(self, tmp_path: Path):
        """Open string (unclosed) is not repairable."""
        malformed = '{"summary":"unclosed string'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_trailing_prose(self, tmp_path: Path):
        """Trailing prose after JSON is not repairable."""
        malformed = '{"summary":"test"} some trailing text'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_unclosed_string_in_array(self, tmp_path: Path):
        """Unclosed string inside array is not repairable."""
        # The array has an unclosed string which is not repairable by adding one character
        # Input ends with "b which is an unclosed string
        malformed = '{"summary":"test","key_points":["a","b}'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_malformed_comma(self, tmp_path: Path):
        """Malformed comma placement is not repairable."""
        # Missing colon between key and value (syntax error)
        malformed = '{"summary""test"}'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_malformed_colon(self, tmp_path: Path):
        """Malformed colon placement is not repairable."""
        malformed = '{"summary":"test","key_points"["a"]}'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error


class TestSharedLimitInvariant:
    """Tests verifying one shared 48 KiB limit across all orchestration paths."""

    def test_runner_chunker_recap_rollup_all_use_same_limit(self):
        """One shared-limit invariant covers runner, chunker default, recap default, and roll-up guard."""
        from hermes_daily_ledger.limits import MAX_MODEL_PROMPT_BYTES

        # Verify chunker's DEFAULT_SAFE_CEILING equals the shared constant
        from hermes_daily_ledger import chunker
        assert chunker.DEFAULT_SAFE_CEILING == MAX_MODEL_PROMPT_BYTES

        # Verify summary orchestrator's safe_ceiling default
        import inspect
        from hermes_daily_ledger.recap_orchestrator import generate_recap
        assert inspect.signature(generate_recap).parameters["safe_ceiling"].default == MAX_MODEL_PROMPT_BYTES

        # Verify roll-up orchestrator's max prompt bytes (module-level constant)
        from hermes_daily_ledger import rollup_orchestrator
        assert rollup_orchestrator._MAX_ROLLUP_PROMPT_BYTES == MAX_MODEL_PROMPT_BYTES

        # Verify runner module uses the shared constant
        from hermes_daily_ledger import auxiliary_runner
        assert auxiliary_runner.MAX_MODEL_PROMPT_BYTES == MAX_MODEL_PROMPT_BYTES

        # Verify limits.py exports ONLY MAX_MODEL_PROMPT_BYTES (no DEFAULT_SAFE_CEILING)
        import hermes_daily_ledger.limits as limits_module
        exported = [name for name in dir(limits_module) if not name.startswith("_")]
        assert "MAX_MODEL_PROMPT_BYTES" in exported
        assert "DEFAULT_SAFE_CEILING" not in exported

        # Verify no local magic values in runner
        runner_source = auxiliary_runner.__file__
        with open(runner_source, "r") as f:
            runner_content = f.read()
        assert "48 * 1024" not in runner_content
        assert "49152" not in runner_content
        assert "_MAX_PROMPT_BYTES" not in runner_content


class TestGenerateSessionSummaryWithAuxiliary:
    """Integration tests ensuring session summary generation uses auxiliary runner."""

    def test_success_uses_auxiliary_runner(self, test_hermes_home, tmp_path: Path):
        """Verify generate_session_summary works when auxiliary runner succeeds."""
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)
        prompts: list[str] = []

        def aux_runner(prompt: str, ledger_root=None) -> AuxiliaryResult:
            """Return a real AuxiliaryResult using the actual runner logic."""
            prompts.append(prompt)
            # Content-only output (summary + key_points only) for per-session contract
            data = {
                "summary": "Test summary",
                "key_points": ["Test point"],
            }
            content = _wrap_with_markers(data)
            response = _create_chat_response(content, model="test")
            from hermes_daily_ledger.auxiliary_runner import _parse_compression_output
            return _parse_compression_output(content, response.model)

        status = generate_session_summary(
            DATE,
            PROFILE,
            SESSION_ID,
            profiles=profiles,
            cron_roots=cron_roots,
            runner=aux_runner,
            ledger_root=tmp_path / "ledger",
        )

        assert status.status == "completed"
        assert len(prompts) == 1

    def test_failure_does_not_publish_artifact(self, test_hermes_home, tmp_path: Path):
        """Verify no summary is published when auxiliary runner fails."""
        home, _ = test_hermes_home
        profiles, cron_roots = discover_all(home)

        def failing_runner(prompt: str, ledger_root=None) -> AuxiliaryResult:
            return AuxiliaryResult(error="Model unavailable")

        status = generate_session_summary(
            DATE,
            PROFILE,
            SESSION_ID,
            profiles=profiles,
            cron_roots=cron_roots,
            runner=failing_runner,
            ledger_root=tmp_path / "ledger",
        )

        assert status.status == "failed"
        assert "Model unavailable" in status.error
        # No summary should be published
        from hermes_daily_ledger.session_storage import load_session_summary
        raw, meta = load_session_summary(DATE, PROFILE, SESSION_ID, tmp_path / "ledger")
        assert raw is None
        assert meta is None


class TestAuxiliaryMetadata:
    """Tests for model routing metadata in generated artifacts."""

    def test_session_summary_records_auxiliary_route(self, test_hermes_home, tmp_path: Path):
        """Verify session summary metadata identifies auxiliary.compression route."""
        from hermes_daily_ledger.session_storage import save_session_summary, load_session_summary

        version = save_session_summary(
            DATE,
            PROFILE,
            SESSION_ID,
            TITLE,
            {"summary": "Test summary"},
            "sha256:test-fingerprint",
            model_profile="auxiliary.compression",
            model="",
            ledger_root=tmp_path / "ledger",
        )

        raw, meta = load_session_summary(DATE, PROFILE, SESSION_ID, tmp_path / "ledger")
        assert meta is not None
        # Verify the metadata identifies the route correctly (auxiliary.compression)
        assert meta.get("model_profile") == "auxiliary.compression"
        assert meta.get("model") == ""


class TestRunAuxiliaryCompressionFocused:
    """Focused tests for run_auxiliary_compression structured output behavior."""

    def test_calls_llm_with_task_compression_and_response_format(self, tmp_path: Path):
        """run_auxiliary_compression calls call_llm with task='compression', messages, extra_body."""
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            data = _make_session_summary_data()
            content = _wrap_with_markers(data)
            return _create_chat_response(content, model="test-model")

        with patch("agent.auxiliary_client.call_llm", mock_call_llm):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert captured_kwargs.get("task") == "compression"
        assert "messages" in captured_kwargs
        assert isinstance(captured_kwargs["messages"], list)
        assert captured_kwargs["messages"] == [{"role": "user", "content": "Test prompt"}]
        assert "extra_body" in captured_kwargs
        assert captured_kwargs["extra_body"] == {"response_format": {"type": "json_object"}}

    def test_succeeds_with_bare_json_response(self, tmp_path: Path):
        """A bare JSON object response (no markers) succeeds."""
        bare_json = json.dumps({
            "session_summaries": [{"profile": PROFILE, "session_id": SESSION_ID, "title": TITLE, "summary": "Test", "key_points": []}],
            "overall_recap": "Test recap",
        })
        response = _create_chat_response(bare_json, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json is not None
        assert "session_summaries" in result.raw_json

    def test_succeeds_with_marker_wrapped_json_response(self, tmp_path: Path):
        """A legacy marker-wrapped JSON response still succeeds."""
        data = _make_session_summary_data()
        content = _wrap_with_markers(data)
        response = _create_chat_response(content, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is None
        assert result.raw_json is not None

    def test_fails_with_truly_malformed_bare_json(self, tmp_path: Path):
        """Truly malformed bare JSON (non-repairable) fails closed."""
        malformed = '{"summary"="missing colon"}'
        response = _create_chat_response(malformed, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_truly_malformed_marker_wrapped_json(self, tmp_path: Path):
        """Truly malformed marker-wrapped JSON (non-repairable) fails closed."""
        malformed = '{"summary"="missing colon"}'
        raw = f"LEDGER_JSON_BEGIN\n{malformed}\nLEDGER_JSON_END"
        response = _create_chat_response(raw, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_fails_with_bare_list_json(self, tmp_path: Path):
        """Bare list JSON fails as non-object."""
        list_json = json.dumps([{"session_id": "s1"}])
        response = _create_chat_response(list_json, model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()

    def test_fails_with_bare_scalar_string(self, tmp_path: Path):
        """Bare scalar string fails as non-object."""
        response = _create_chat_response('"just a string"', model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()

    def test_fails_with_bare_null(self, tmp_path: Path):
        """Bare null fails as non-object."""
        response = _create_chat_response("null", model="test-model")

        with patch("agent.auxiliary_client.call_llm", return_value=response):
            result = run_auxiliary_compression(
                prompt="Test prompt",
                ledger_root=tmp_path / "ledger",
            )

        assert result.error is not None
        assert "non-object" in result.error.lower() or "object" in result.error.lower()
