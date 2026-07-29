"""Tests for token usage tracking feature."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents._logging import log_raw_sdk_event
from coder_eval.agents.claude_code_agent import (
    ClaudeCodeAgent,
    _is_sdk_result_message,
    _is_task_notification,
)
from coder_eval.errors import AgentCrashError
from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    CommandTelemetry,
    EvaluationResult,
    RunSummary,
    TokenUsage,
    TurnRecord,
    UserMessage,
    parse_agent_config,
)
from coder_eval.pricing import calculate_cost
from coder_eval.reports import ReportGenerator


def _assistant(
    *,
    message_id: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> AssistantMessage:
    """Build an AssistantMessage telemetry record with the given per-call tokens."""
    now = datetime.now()
    return AssistantMessage(
        started_at=now,
        completed_at=now,
        generation_duration_ms=1.0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        message_id=message_id,
    )


# --- TokenUsage model tests ---


class TestTokenUsageModel:
    """Tests for the TokenUsage Pydantic model."""

    def test_construction_with_all_fields(self):
        usage = TokenUsage(
            uncached_input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
            total_cost_usd=0.0123,
        )
        assert usage.uncached_input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cache_creation_input_tokens == 200
        assert usage.cache_read_input_tokens == 100
        assert usage.total_cost_usd == 0.0123

    def test_defaults_all_zeros(self):
        usage = TokenUsage()
        assert usage.uncached_input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.total_cost_usd is None

    def test_total_tokens_property(self):
        usage = TokenUsage(uncached_input_tokens=1000, output_tokens=500)
        assert usage.total_tokens == 1500

    def test_total_tokens_zero(self):
        usage = TokenUsage()
        assert usage.total_tokens == 0

    def test_serialization_roundtrip(self):
        original = TokenUsage(
            uncached_input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
            total_cost_usd=0.05,
        )
        json_str = original.model_dump_json()
        restored = TokenUsage.model_validate_json(json_str)

        assert restored.uncached_input_tokens == original.uncached_input_tokens
        assert restored.output_tokens == original.output_tokens
        assert restored.cache_creation_input_tokens == original.cache_creation_input_tokens
        assert restored.cache_read_input_tokens == original.cache_read_input_tokens
        assert restored.total_cost_usd == original.total_cost_usd

    def test_serialization_excludes_computed_property(self):
        usage = TokenUsage(uncached_input_tokens=100, output_tokens=50)
        dumped = usage.model_dump()
        # total_tokens is a @property, not a field, so it shouldn't be in dump
        assert "total_tokens" not in dumped


# --- CommandTelemetry.result_tokens tests ---


class TestCommandTelemetryResultTokens:
    """The content-based, cache-independent tool-output size (ceil(len/4))."""

    @staticmethod
    def _telemetry(result_summary: str | None) -> CommandTelemetry:
        return CommandTelemetry(
            tool_name="Bash",
            tool_id="t1",
            timestamp=datetime.now(),
            result_summary=result_summary,
        )

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            (None, 0),  # no result content
            ("", 0),  # empty summary
            ("a", 1),  # 1 char -> ceil(1/4) = 1
            ("abcd", 1),  # exact multiple -> ceil(4/4) = 1
            ("abcde", 2),  # 5 chars -> ceil(5/4) = 2
            ("a" * 8, 2),  # exact multiple -> ceil(8/4) = 2
            ("a" * 9, 3),  # 9 chars -> ceil(9/4) = 3
        ],
    )
    def test_result_tokens_ceil(self, summary: str | None, expected: int):
        assert self._telemetry(summary).result_tokens == expected

    def test_result_tokens_is_computed_not_a_stored_field(self):
        # It is a @computed_field: present in the dump but not settable as input.
        tel = self._telemetry("abcde")
        assert tel.model_dump()["result_tokens"] == 2


# --- TurnRecord token_usage field tests ---


class TestTurnRecordTokenUsage:
    """Tests for token_usage field on TurnRecord."""

    def test_turn_record_default_none(self):
        record = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
        )
        assert record.token_usage is None

    def test_turn_record_with_token_usage(self):
        usage = TokenUsage(uncached_input_tokens=500, output_tokens=200, total_cost_usd=0.01)
        record = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            token_usage=usage,
        )
        assert record.token_usage is not None
        assert record.token_usage.total_tokens == 700

    def test_turn_record_serialization_with_token_usage(self):
        usage = TokenUsage(uncached_input_tokens=500, output_tokens=200)
        record = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            token_usage=usage,
        )
        json_str = record.model_dump_json()
        restored = TurnRecord.model_validate_json(json_str)
        assert restored.token_usage is not None
        assert restored.token_usage.uncached_input_tokens == 500


# --- EvaluationResult total_token_usage field tests ---


class TestEvaluationResultTokenUsage:
    """Tests for total_token_usage field on EvaluationResult."""

    def test_evaluation_result_default_none(self):
        result = EvaluationResult(
            task_id="test",
            task_description="test task",
            variant_id="test-variant",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
        )
        assert result.total_token_usage is None

    def test_evaluation_result_with_total_token_usage(self):
        usage = TokenUsage(
            uncached_input_tokens=3000,
            output_tokens=1500,
            total_cost_usd=0.05,
        )
        result = EvaluationResult(
            task_id="test",
            task_description="test task",
            variant_id="test-variant",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            total_token_usage=usage,
        )
        assert result.total_token_usage is not None
        assert result.total_token_usage.total_tokens == 4500


# --- Agent type guard tests ---


class TestSdkResultMessageGuard:
    """Tests for _is_sdk_result_message type guard."""

    def test_true_for_sdk_result_message(self):
        # spec so the mock doesn't auto-vivify task_id/status/tool_use_id, which
        # would make it look like a TaskNotification (a real ResultMessage has none).
        msg = MagicMock(spec=["session_id", "usage", "num_turns", "total_cost_usd"])
        msg.session_id = "sess-123"
        msg.usage = {"input_tokens": 100, "output_tokens": 50}
        assert _is_sdk_result_message(msg) is True

    def test_false_for_tool_result_block(self):
        msg = MagicMock(spec=["tool_use_id", "is_error", "content"])
        # Ensure it does NOT have session_id/usage
        del msg.session_id
        del msg.usage
        assert _is_sdk_result_message(msg) is False

    def test_false_for_assistant_message(self):
        msg = MagicMock(spec=["content", "role"])
        assert _is_sdk_result_message(msg) is False

    def test_task_notification_not_misread_as_result(self):
        """A TaskNotification has session_id + usage (so it looks like a
        ResultMessage) but must be excluded — else its TaskUsage would clobber
        the real ResultMessage usage/model_usage."""
        tn = MagicMock(spec=["session_id", "usage", "subtype", "task_id", "status", "tool_use_id"])
        tn.subtype = "task_notification"
        assert _is_task_notification(tn) is True
        assert _is_sdk_result_message(tn) is False

    def test_real_result_is_not_a_task_notification(self):
        result = MagicMock(spec=["session_id", "usage", "num_turns", "total_cost_usd", "model_usage"])
        assert _is_task_notification(result) is False
        assert _is_sdk_result_message(result) is True


# --- _build_token_usage source-of-truth tests ---


class TestBuildTokenUsage:
    """Tests for ClaudeCodeAgent._build_token_usage.

    The per-call telemetry stream (deduped by message_id) is the authoritative
    cumulative source. ResultMessage.usage is a non-cumulative snapshot that
    under-reports cache/input on multi-call runs, so it is only a fallback.
    """

    def test_sums_per_call_stream_over_resultmessage(self):
        """Cumulative cache reads come from summing the per-call stream, not the
        ResultMessage snapshot — the regression the bug surfaced."""
        messages = [
            _assistant(message_id="m1", input_tokens=10, output_tokens=100, cache_read_tokens=20_000),
            _assistant(message_id="m2", input_tokens=5, output_tokens=80, cache_read_tokens=40_000),
            _assistant(message_id="m3", input_tokens=3, output_tokens=60, cache_read_tokens=60_000),
        ]
        # ResultMessage snapshot drastically understates cache_read (e.g. only
        # the final call's 60k) — must NOT win.
        sdk_usage = {
            "input_tokens": 3,
            "output_tokens": 240,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 60_000,
        }
        usage = ClaudeCodeAgent._build_token_usage(messages, sdk_usage, 1.23)
        assert usage is not None
        assert usage.cache_read_input_tokens == 120_000  # 20k + 40k + 60k, not 60k
        assert usage.uncached_input_tokens == 18
        assert usage.output_tokens == 240
        # total_cost_usd is always the real billed total from the ResultMessage.
        assert usage.total_cost_usd == 1.23

    def test_deduped_followups_do_not_double_count(self):
        """Follow-up emissions sharing a message_id are recorded as zeros by the
        recorder, so summing the stream stays exact."""
        messages = [
            _assistant(message_id="m1", input_tokens=10, output_tokens=50, cache_read_tokens=30_000),
            # Same API call, second content block — recorder zeroed its usage.
            _assistant(message_id="m1", input_tokens=0, output_tokens=25, cache_read_tokens=0),
            _assistant(message_id="m2", input_tokens=4, output_tokens=40, cache_read_tokens=45_000),
        ]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, 0.5)
        assert usage is not None
        assert usage.uncached_input_tokens == 14
        assert usage.output_tokens == 115  # 50 + 25 + 40
        assert usage.cache_read_input_tokens == 75_000

    def test_falls_back_to_resultmessage_when_no_per_message_tokens(self):
        """Streams that surface no per-call tokens (ResultMessage only) keep the
        legacy behavior."""
        usage = ClaudeCodeAgent._build_token_usage(
            [],
            {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 100,
            },
            0.02,
        )
        assert usage is not None
        assert usage.uncached_input_tokens == 1000
        assert usage.cache_read_input_tokens == 100
        assert usage.total_cost_usd == 0.02

    def test_falls_back_when_token_bearing_message_lacks_id(self):
        """Legacy/mock streams whose token-bearing emissions have no message_id
        defer to ResultMessage (summing them would double-count the backfill)."""
        messages = [
            _assistant(message_id=None, input_tokens=999, output_tokens=999, cache_read_tokens=999_999),
        ]
        sdk_usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
        }
        usage = ClaudeCodeAgent._build_token_usage(messages, sdk_usage, None)
        assert usage is not None
        # ResultMessage values, NOT the id-less per-message values.
        assert usage.uncached_input_tokens == 1000
        assert usage.cache_read_input_tokens == 100

    def test_ignores_user_messages_when_summing(self):
        """Only assistant emissions carry billing; user telemetry is skipped."""
        now = datetime.now()
        messages = [
            UserMessage(text="hi", started_at=now, completed_at=now),
            _assistant(message_id="m1", input_tokens=10, output_tokens=20, cache_read_tokens=5_000),
        ]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None)
        assert usage is not None
        assert usage.uncached_input_tokens == 10
        assert usage.cache_read_input_tokens == 5_000

    def test_returns_none_when_nothing_available(self):
        assert ClaudeCodeAgent._build_token_usage([], None, None) is None

    def test_model_usage_is_authoritative_over_stream_and_snapshot(self):
        """ResultMessage.model_usage (cumulative per-model) wins — it captures
        sub-agent cache-creation/input the stream and snapshot under-report."""
        # Stream + snapshot both say cache_creation ~21873; model_usage knows the
        # true cumulative 51339 (the merge-sort probe's real numbers).
        messages = [_assistant(message_id="m1", input_tokens=110, output_tokens=1800, cache_creation_tokens=21873)]
        snapshot = {
            "input_tokens": 110,
            "output_tokens": 1800,
            "cache_creation_input_tokens": 21873,
            "cache_read_input_tokens": 41844,
        }
        model_usage = {
            "us.anthropic.claude-sonnet-4-6": {
                "inputTokens": 834,
                "outputTokens": 1834,
                "cacheReadInputTokens": 41844,
                "cacheCreationInputTokens": 51339,
                "costUSD": 0.23508645,
            }
        }
        usage = ClaudeCodeAgent._build_token_usage(messages, snapshot, 0.23508645, model_usage)
        assert usage is not None
        assert usage.uncached_input_tokens == 834
        assert usage.output_tokens == 1834
        assert usage.cache_creation_input_tokens == 51339  # not 21873
        assert usage.cache_read_input_tokens == 41844
        # cost comes from summed costUSD, and reconciles with total_cost_usd.
        assert usage.total_cost_usd == pytest.approx(0.23508645)

    def test_model_usage_sums_across_multiple_models(self):
        model_usage = {
            "model-a": {"inputTokens": 100, "outputTokens": 200, "costUSD": 0.01},
            "model-b": {"inputTokens": 5, "outputTokens": 7, "cacheCreationInputTokens": 9, "costUSD": 0.02},
        }
        usage = ClaudeCodeAgent._build_token_usage([], None, None, model_usage)
        assert usage is not None
        assert usage.uncached_input_tokens == 105
        assert usage.output_tokens == 207
        assert usage.cache_creation_input_tokens == 9
        assert usage.total_cost_usd == pytest.approx(0.03)

    def test_model_usage_without_cost_falls_back_to_result_cost(self):
        model_usage = {"m": {"inputTokens": 10, "outputTokens": 20}}  # no costUSD
        usage = ClaudeCodeAgent._build_token_usage([], None, 0.5, model_usage)
        assert usage is not None
        assert usage.uncached_input_tokens == 10
        assert usage.total_cost_usd == 0.5

    def test_empty_model_usage_falls_through_to_stream(self):
        # Empty/absent model_usage must not shadow the stream-sum path.
        messages = [_assistant(message_id="m1", input_tokens=7, cache_read_tokens=300)]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None, {})
        assert usage is not None
        assert usage.uncached_input_tokens == 7
        assert usage.cache_read_input_tokens == 300

    def test_timeout_turn_backfills_cost_from_buckets(self):
        """A killed/timed-out turn has no ResultMessage (no SDK cost), but its
        tokens are captured. The cost is backfilled from the rate card so it
        records a number instead of None (issue #386)."""
        messages = [
            _assistant(
                message_id="m1",
                input_tokens=1_000_000,
                output_tokens=500_000,
                cache_creation_tokens=200_000,
                cache_read_tokens=4_000_000,
            ),
        ]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None, None, "claude-opus-4-8")
        assert usage is not None
        # opus-4-8: 15/M in, 75/M out, 18.75/M cache-write, 1.50/M cache-read.
        expected = (1_000_000 * 15.0 + 500_000 * 75.0 + 200_000 * 18.75 + 4_000_000 * 1.50) / 1_000_000
        assert usage.total_cost_usd == pytest.approx(expected)

    def test_backfill_via_resultmessage_snapshot_path(self):
        """The snapshot-only fallback path (no per-message tokens) is also priced
        when the SDK cost is absent. Expected cost comes from ``calculate_cost``
        itself (not re-pinned literals) — this asserts the buckets are wired to
        pricing correctly without duplicating the rate card."""
        usage = ClaudeCodeAgent._build_token_usage(
            [],
            {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 100},
            None,
            None,
            "claude-opus-4-8",
        )
        assert usage is not None
        expected = calculate_cost(
            "claude-opus-4-8", uncached_input_tokens=1000, output_tokens=500, cache_read_tokens=100
        )
        assert usage.total_cost_usd == pytest.approx(expected)

    def test_sdk_cost_not_overwritten_by_backfill(self):
        """When the SDK supplied a cost, the backfill is a no-op (real billed
        total wins over the rate-card estimate)."""
        messages = [_assistant(message_id="m1", input_tokens=10, output_tokens=20)]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, 1.23, None, "claude-opus-4-8")
        assert usage is not None
        assert usage.total_cost_usd == 1.23

    def test_backfill_noop_for_unknown_model(self):
        """An unpriced/unknown model leaves cost None — no crash, no fabrication."""
        messages = [_assistant(message_id="m1", input_tokens=10, output_tokens=20)]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None, None, "totally-unknown-model")
        assert usage is not None
        assert usage.total_cost_usd is None

    def test_backfill_noop_when_no_model_provided(self):
        """Without a model id the cost stays None (back-compat with old callers)."""
        messages = [_assistant(message_id="m1", input_tokens=10, output_tokens=20)]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None)
        assert usage is not None
        assert usage.total_cost_usd is None


# --- Agent token capture tests ---


class TestAgentTokenCapture:
    """Tests for token usage capture in ClaudeCodeAgent.communicate()."""

    @pytest.mark.asyncio
    async def test_communicate_captures_token_usage(self):
        """Verify that when SDK yields a ResultMessage with usage, TurnRecord.token_usage is populated."""
        config = parse_agent_config(
            type=AgentKind.CLAUDE_CODE,
            permission_mode="acceptEdits",
            allowed_tools=["Read"],
        )
        agent = ClaudeCodeAgent(config)
        agent.working_directory = MagicMock()
        agent.working_directory.rglob.return_value = []

        # Create a mock SDK ResultMessage (final message with usage data)
        sdk_result = MagicMock()
        sdk_result.session_id = "sess-abc"
        sdk_result.usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 100,
        }
        sdk_result.total_cost_usd = 0.0234
        # Ensure it doesn't match other guards
        del sdk_result.content
        del sdk_result.model
        del sdk_result.tool_use_id
        del sdk_result.is_error
        del sdk_result.name
        del sdk_result.id
        del sdk_result.input

        # Mock the query function to yield our SDK result message
        async def mock_query(*args, **kwargs):
            yield sdk_result

        with patch("coder_eval.agents.claude_code_agent.query", side_effect=mock_query):
            record = await agent.communicate("test prompt")

        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 1000
        assert record.token_usage.output_tokens == 500
        assert record.token_usage.cache_creation_input_tokens == 200
        assert record.token_usage.cache_read_input_tokens == 100
        assert record.token_usage.total_cost_usd == 0.0234
        # total_tokens = full prompt (uncached 1000 + cc 200 + cr 100 = 1300) + output 500
        assert record.token_usage.input_tokens == 1300
        assert record.token_usage.total_tokens == 1800

    @pytest.mark.asyncio
    async def test_communicate_no_usage_when_not_present(self):
        """Verify that TurnRecord.token_usage is None when SDK doesn't yield usage."""
        config = parse_agent_config(
            type=AgentKind.CLAUDE_CODE,
            permission_mode="acceptEdits",
            allowed_tools=["Read"],
        )
        agent = ClaudeCodeAgent(config)
        agent.working_directory = MagicMock()
        agent.working_directory.rglob.return_value = []

        # Create a mock assistant message (no usage data)
        assistant_msg = MagicMock()
        assistant_msg.content = "Hello"
        assistant_msg.model = "mock-model"
        # Remove attributes so it doesn't match SDK result guard
        del assistant_msg.session_id
        del assistant_msg.usage

        async def mock_query(*args, **kwargs):
            yield assistant_msg

        with patch("coder_eval.agents.claude_code_agent.query", side_effect=mock_query):
            record = await agent.communicate("test prompt")

        assert record.token_usage is None

    @pytest.mark.asyncio
    async def test_crashed_turn_backfills_cost_end_to_end(self):
        """End-to-end wiring (issue #386): on a crash there is no ResultMessage,
        so the SDK supplies no cost. The agent must thread its resolved
        ``effective_model`` through ``communicate() → _finalize →
        _build_token_usage`` so the partial ``pending_turn`` records a
        rate-card cost instead of None. The unit tests for ``_build_token_usage``
        pass an explicit model; this proves the closure is actually wired."""
        config = parse_agent_config(
            type=AgentKind.CLAUDE_CODE,
            permission_mode="acceptEdits",
            allowed_tools=["Read"],
            model="claude-opus-4-8",
        )
        agent = ClaudeCodeAgent(config)
        agent.working_directory = MagicMock()
        agent.working_directory.rglob.return_value = []

        # A token-bearing assistant emission, then a generic crash before any
        # terminal ResultMessage (so sdk_result_cost stays None).
        assistant_msg = SimpleNamespace(
            content=[SimpleNamespace(text="working on it")],
            model="claude-opus-4-8",
            message_id="m1",
            usage={
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 100,
            },
        )

        async def mock_query(*args, **kwargs):
            yield assistant_msg
            raise RuntimeError("subprocess died mid-turn")

        with (
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport", return_value=MagicMock()),
            patch("coder_eval.agents.claude_code_agent.query", side_effect=mock_query),
            pytest.raises(AgentCrashError),
        ):
            await agent.communicate("test prompt")

        # The crashed partial turn is parked on pending_turn with cost backfilled.
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        usage = agent.pending_turn.token_usage
        assert usage is not None
        expected = calculate_cost(
            "claude-opus-4-8",
            uncached_input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=100,
        )
        assert usage.total_cost_usd == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_communicate_handles_none_cache_tokens(self):
        """Verify that None cache tokens in usage dict are treated as 0."""
        config = parse_agent_config(
            type=AgentKind.CLAUDE_CODE,
            permission_mode="acceptEdits",
            allowed_tools=["Read"],
        )
        agent = ClaudeCodeAgent(config)
        agent.working_directory = MagicMock()
        agent.working_directory.rglob.return_value = []

        sdk_result = MagicMock()
        sdk_result.session_id = "sess-abc"
        sdk_result.usage = {
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }
        sdk_result.total_cost_usd = None
        del sdk_result.content
        del sdk_result.model
        del sdk_result.tool_use_id
        del sdk_result.is_error
        del sdk_result.name
        del sdk_result.id
        del sdk_result.input

        async def mock_query(*args, **kwargs):
            yield sdk_result

        with patch("coder_eval.agents.claude_code_agent.query", side_effect=mock_query):
            record = await agent.communicate("test prompt")

        assert record.token_usage is not None
        assert record.token_usage.cache_creation_input_tokens == 0
        assert record.token_usage.cache_read_input_tokens == 0
        assert record.token_usage.total_cost_usd is None

    @pytest.mark.asyncio
    async def test_subagent_terminal_synthesized_and_total_stays_model_usage(self):
        """End-to-end through communicate(): a sub-agent Agent-tool result is
        synthesized into a ``parent_tool_use_id`` message, AND the turn total
        stays exactly ``model_usage`` — i.e. the synthetic message does NOT
        inflate ``token_usage`` (no double-count)."""
        config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits", allowed_tools=["Read"])
        agent = ClaudeCodeAgent(config)
        agent.working_directory = MagicMock()
        agent.working_directory.rglob.return_value = []

        # Sub-agent Agent-tool result (its terminal generation) → triggers synthesis.
        block = MagicMock()
        block.tool_use_id = "tool_sub"
        block.is_error = False
        block.content = "5050"
        user_msg = MagicMock()
        user_msg.content = [block]
        user_msg.tool_use_result = {
            "agentId": "a1",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 5,
                "cache_creation_input_tokens": 112,
                "cache_read_input_tokens": 14536,
            },
        }
        del user_msg.model  # keep out of the assistant branch
        del user_msg.session_id  # keep out of the result-message branch

        # ResultMessage carrying the authoritative cumulative total (model_usage).
        result = MagicMock(spec=["session_id", "usage", "num_turns", "total_cost_usd", "model_usage"])
        result.session_id = "s1"
        result.usage = {
            "input_tokens": 521,
            "output_tokens": 481,
            "cache_creation_input_tokens": 35329,
            "cache_read_input_tokens": 55242,
        }
        result.model_usage = {
            "claude-x": {
                "inputTokens": 521,
                "outputTokens": 481,
                "cacheCreationInputTokens": 35329,
                "cacheReadInputTokens": 55242,
                "costUSD": 0.15,
            }
        }
        result.num_turns = 1
        result.total_cost_usd = 0.15

        async def mock_query(*args, **kwargs):
            yield user_msg
            yield result

        with patch("coder_eval.agents.claude_code_agent.query", side_effect=mock_query):
            record = await agent.communicate("delegate it")

        # Turn total is the model_usage figure — UNCHANGED by the synthetic message.
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 521
        assert record.token_usage.output_tokens == 481
        assert record.token_usage.cache_creation_input_tokens == 35329
        assert record.token_usage.cache_read_input_tokens == 55242
        assert record.token_usage.total_cost_usd == 0.15

        # The sub-agent's terminal generation was synthesized as a nested message,
        # carrying its own tokens — so grouping by parent_tool_use_id recovers it.
        sub = [m for m in record.messages if getattr(m, "parent_tool_use_id", None) == "tool_sub"]
        assert len(sub) == 1
        assert sub[0].output_tokens == 5
        assert sub[0].cache_read_tokens == 14536
        assert sub[0].cache_creation_tokens == 112
        assert sub[0].content_blocks[0].text == "5050"


# --- Orchestrator aggregation tests ---


class TestOrchestratorTokenAggregation:
    """Tests for token usage aggregation logic (unit tests for the aggregation algorithm)."""

    def test_aggregate_multiple_turns(self):
        """Test aggregation of token usage across multiple turns."""
        turns = [
            TurnRecord(
                iteration=1,
                user_input="p1",
                agent_output="r1",
                token_usage=TokenUsage(uncached_input_tokens=1000, output_tokens=500, total_cost_usd=0.01),
            ),
            TurnRecord(
                iteration=2,
                user_input="p2",
                agent_output="r2",
                token_usage=TokenUsage(uncached_input_tokens=2000, output_tokens=800, total_cost_usd=0.02),
            ),
        ]

        turns_with_usage = [t for t in turns if t.token_usage]
        costs = [t.token_usage.total_cost_usd for t in turns_with_usage if t.token_usage.total_cost_usd is not None]
        aggregated = TokenUsage(
            uncached_input_tokens=sum(t.token_usage.uncached_input_tokens for t in turns_with_usage),
            output_tokens=sum(t.token_usage.output_tokens for t in turns_with_usage),
            cache_creation_input_tokens=sum(t.token_usage.cache_creation_input_tokens for t in turns_with_usage),
            cache_read_input_tokens=sum(t.token_usage.cache_read_input_tokens for t in turns_with_usage),
            total_cost_usd=sum(costs) if costs else None,
        )

        assert aggregated.uncached_input_tokens == 3000
        assert aggregated.output_tokens == 1300
        assert aggregated.total_tokens == 4300
        assert aggregated.total_cost_usd == pytest.approx(0.03)

    def test_aggregate_mixed_turns(self):
        """Test aggregation with some turns missing token usage."""
        turns = [
            TurnRecord(
                iteration=1,
                user_input="p1",
                agent_output="r1",
                token_usage=TokenUsage(uncached_input_tokens=1000, output_tokens=500, total_cost_usd=0.01),
            ),
            TurnRecord(
                iteration=2,
                user_input="p2",
                agent_output="r2",
                token_usage=None,  # No usage data
            ),
            TurnRecord(
                iteration=3,
                user_input="p3",
                agent_output="r3",
                token_usage=TokenUsage(uncached_input_tokens=800, output_tokens=400),
            ),
        ]

        turns_with_usage = [t for t in turns if t.token_usage]
        assert len(turns_with_usage) == 2

        costs = [t.token_usage.total_cost_usd for t in turns_with_usage if t.token_usage.total_cost_usd is not None]
        aggregated = TokenUsage(
            uncached_input_tokens=sum(t.token_usage.uncached_input_tokens for t in turns_with_usage),
            output_tokens=sum(t.token_usage.output_tokens for t in turns_with_usage),
            total_cost_usd=sum(costs) if costs else None,
        )

        assert aggregated.uncached_input_tokens == 1800
        assert aggregated.output_tokens == 900
        assert aggregated.total_cost_usd == pytest.approx(0.01)

    def test_aggregate_no_usage(self):
        """Test that no turns with usage yields no aggregation."""
        turns = [
            TurnRecord(iteration=1, user_input="p1", agent_output="r1", token_usage=None),
        ]

        turns_with_usage = [t for t in turns if t.token_usage]
        assert len(turns_with_usage) == 0


# --- Report generation tests ---


class TestReportTokenUsageSection:
    """Tests for token usage section in report generation."""

    def test_token_section_renders_with_data(self):
        task_results = [
            {
                "task_id": "task1",
                "input_tokens": 3000,
                "output_tokens": 2000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 10000,
                "total_tokens": 5000,
                "total_cost_usd": 0.05,
            },
            {
                "task_id": "task2",
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 8000,
                "total_tokens": 3000,
                "total_cost_usd": 0.03,
            },
        ]
        lines = ReportGenerator._generate_token_usage_section(task_results)
        joined = "\n".join(lines)

        assert len(lines) > 0
        assert "## Token Usage" in lines
        assert "**Total Tokens**: 8,000 (input: 5,000, output: 3,000)" in joined
        assert "**Cache Tokens**: write: 500, read: 18,000" in joined
        assert "**Total Cost**: $0.0800" in joined
        assert "**Avg Tokens/Task**: 4,000" in joined
        assert "| task1 | 3,000 | 2,000 | 500 | 10,000 | 5,000 | $0.0500 |" in joined
        assert "| task2 | 2,000 | 1,000 | 0 | 8,000 | 3,000 | $0.0300 |" in joined

    def test_token_section_omitted_when_no_data(self):
        task_results = [
            {"task_id": "task1", "total_tokens": None, "total_cost_usd": None},
        ]
        lines = ReportGenerator._generate_token_usage_section(task_results)
        assert len(lines) == 0

    def test_token_section_handles_missing_cost(self):
        task_results = [
            {
                "task_id": "task1",
                "input_tokens": 3000,
                "output_tokens": 2000,
                "total_tokens": 5000,
                "total_cost_usd": None,
                "cost_complete": False,
            },
        ]
        lines = ReportGenerator._generate_token_usage_section(task_results)
        joined = "\n".join(lines)

        assert "## Token Usage" in joined
        assert "**Total Tokens**: 5,000 (input: 3,000, output: 2,000)" in joined
        # Tokens were burned, so there IS a bill — the report must say the number is
        # missing rather than omit the cost line, which reads as "this run was free".
        assert "**Total Cost**: unavailable" in joined
        assert "1 task(s) burned tokens the rate card could not price" in joined
        assert "| task1 | 3,000 | 2,000 | 0 | 0 | 5,000 | N/A |" in joined

    def test_token_section_leaves_a_legacy_row_uncaveated(self):
        """A row predating ``cost_complete`` is read as priced, not inferred.

        The same shape as above minus the flag, which is what runs written before
        the field look like. Deliberately silent: inferring unpriced-ness from
        tokens would give old runs a caveat at the price of a second definition of
        "unpriced", and every new run carries the flag.
        """
        task_results = [
            {"task_id": "task1", "total_tokens": 5000, "total_cost_usd": None},
        ]
        joined = "\n".join(ReportGenerator._generate_token_usage_section(task_results))

        assert "## Token Usage" in joined
        assert "could not price" not in joined

    def test_token_section_omits_cost_when_no_tokens_burned(self):
        """A row that burned nothing is genuinely free — no unpriced warning."""
        task_results = [
            {
                "task_id": "task1",
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": None,
            },
        ]
        joined = "\n".join(ReportGenerator._generate_token_usage_section(task_results))

        assert "## Token Usage" in joined
        assert "**Total Cost**" not in joined

    def test_token_section_marks_partial_cost_as_floor(self):
        """A priced row beside an unpriced one reports the total as a floor.

        The failure this guards: summing only the rows that carry a cost and
        presenting it as the run's bill. The unpriced rows are typically the
        killed ones, which are also the ones that burned the most getting there.
        """
        task_results = [
            {"task_id": "priced", "total_tokens": 1000, "total_cost_usd": 0.25, "cost_complete": True},
            {"task_id": "unpriced", "total_tokens": 4_000_000, "total_cost_usd": None, "cost_complete": False},
        ]
        joined = "\n".join(ReportGenerator._generate_token_usage_section(task_results))

        assert "**Total Cost**: $0.2500 (floor" in joined
        assert "1 task(s) burned tokens the rate card could not price" in joined

    def test_token_section_breaks_out_eval_overhead(self):
        """Judge spend is reported beside the agent bill, and folded into the total."""
        task_results = [
            {
                "task_id": "task1",
                "total_tokens": 1000,
                "total_cost_usd": 1.0,
                "cost_complete": True,
                "judge_cost_usd": 0.25,
            },
        ]
        joined = "\n".join(ReportGenerator._generate_token_usage_section(task_results))

        assert "**Agent Cost**: $1.0000" in joined
        assert "**Eval Overhead (judge + simulator)**: $0.2500" in joined
        assert "**Total Cost**: $1.2500" in joined

    def test_token_section_in_full_report(self):
        """Test that token section appears in generate_markdown when data is available."""
        summary = RunSummary(
            run_id="test-run",
            start_time=datetime(2025, 10, 11, 12, 0, 0),
            end_time=datetime(2025, 10, 11, 12, 1, 0),
            total_duration_seconds=60.0,
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            task_results=[
                {
                    "task_id": "task1",
                    "status": "SUCCESS",
                    "weighted_score": 1.0,
                    "duration": 30.0,
                    "iteration_count": 1,
                    "iterations": [],
                    "reference_similarity": None,
                    "input_tokens": 7000,
                    "output_tokens": 3000,
                    "total_tokens": 10000,
                    "total_cost_usd": 0.1234,
                }
            ],
            framework_version="0.1.0",
            environment_info={},
        )

        report_md = ReportGenerator.generate_markdown(summary)
        assert "## Token Usage" in report_md
        assert "10,000" in report_md
        assert "input: 7,000" in report_md
        assert "output: 3,000" in report_md
        assert "$0.1234" in report_md

    def test_token_section_handles_zero_cost(self):
        """Test that $0.0000 cost is displayed correctly (not treated as missing)."""
        task_results = [
            {
                "task_id": "task1",
                "input_tokens": 3000,
                "output_tokens": 2000,
                "total_tokens": 5000,
                "total_cost_usd": 0.0,
            },
        ]
        lines = ReportGenerator._generate_token_usage_section(task_results)
        joined = "\n".join(lines)

        assert "**Total Cost**: $0.0000" in joined
        assert "| task1 | 3,000 | 2,000 | 0 | 0 | 5,000 | $0.0000 |" in joined

    def test_token_section_not_in_full_report_without_data(self):
        """Test that token section is absent when no token data."""
        summary = RunSummary(
            run_id="test-run",
            start_time=datetime(2025, 10, 11, 12, 0, 0),
            end_time=datetime(2025, 10, 11, 12, 1, 0),
            total_duration_seconds=60.0,
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            task_results=[
                {
                    "task_id": "task1",
                    "status": "SUCCESS",
                    "weighted_score": 1.0,
                    "duration": 30.0,
                }
            ],
            framework_version="0.1.0",
            environment_info={},
        )

        report_md = ReportGenerator.generate_markdown(summary)
        assert "## Token Usage" not in report_md


# --- _synthesize_subagent_terminal_message tests ---


class TestSynthesizeSubagentTerminalMessage:
    """Tests for ClaudeCodeAgent._synthesize_subagent_terminal_message.

    Materializes a sub-agent's terminal generation (delivered as the Agent tool
    result, never streamed) as a ``parent_tool_use_id``-tagged AssistantMessage,
    so per-sub-agent usage is recoverable by grouping messages on that id.
    """

    def _make_msg(
        self, tool_use_result: dict | None, tool_use_id: str = "toolu_123", result_content: object = None
    ) -> MagicMock:
        msg = MagicMock()
        msg.tool_use_result = tool_use_result
        block = MagicMock()
        block.tool_use_id = tool_use_id
        block.is_error = False
        block.content = result_content
        msg.content = [block]
        return msg

    def test_builds_message_with_full_breakdown(self):
        msg = self._make_msg(
            {
                "agentId": "agent-abc",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 1500,
                },
                "status": "completed",
            },
            result_content="answer: 5050",
        )
        result = ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, "claude-x")
        assert result is not None
        assert result.input_tokens == 10
        assert result.output_tokens == 50
        assert result.cache_creation_tokens == 200
        assert result.cache_read_tokens == 1500
        # Parented to the spawning Agent call so message-grouping attributes it.
        assert result.parent_tool_use_id == "toolu_123"
        assert result.model == "claude-x"
        # The sub-agent's returned text becomes a text content block.
        assert result.content_blocks and result.content_blocks[0].text == "answer: 5050"

    def test_returns_none_for_non_agent_tool(self):
        # Bash/Read/Write results have no agentId
        msg = self._make_msg({"status": "completed", "output": "hello"})
        assert ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None) is None

    def test_returns_none_when_tool_use_result_missing(self):
        msg = MagicMock()
        msg.tool_use_result = None
        assert ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None) is None

    def test_returns_none_when_usage_missing(self):
        msg = self._make_msg({"agentId": "agent-abc"})  # no usage key
        assert ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None) is None

    def test_returns_none_without_tool_use_id(self):
        # No ToolResultBlock → no Agent tool_use_id to parent under → cannot attribute.
        msg = MagicMock()
        msg.tool_use_result = {"agentId": "agent-abc", "usage": {"output_tokens": 5}}
        msg.content = []
        assert ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None) is None

    def test_coerces_missing_token_fields_to_zero(self):
        msg = self._make_msg({"agentId": "agent-abc", "usage": {}, "status": "completed"})
        result = ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None)
        assert result is not None
        assert (result.input_tokens, result.output_tokens) == (0, 0)
        assert (result.cache_creation_tokens, result.cache_read_tokens) == (0, 0)

    def test_coerces_none_token_fields_to_zero(self):
        msg = self._make_msg(
            {
                "agentId": "agent-abc",
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                },
            }
        )
        result = ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None)
        assert result is not None
        assert result.input_tokens == 0 and result.output_tokens == 0

    def test_no_text_block_when_result_empty(self):
        # Tokens are still captured; an empty returned text yields no content block.
        msg = self._make_msg({"agentId": "agent-abc", "usage": {"output_tokens": 5}})
        result = ClaudeCodeAgent._synthesize_subagent_terminal_message(msg, None)
        assert result is not None
        assert result.output_tokens == 5
        assert result.content_blocks == []


# --- log_raw_sdk_event env-gate tests (shared by both agents) ---


def _make_agent() -> ClaudeCodeAgent:
    return ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))


class TestLogRawSdkEvent:
    def test_disabled_by_default(self, caplog):
        import os

        os.environ.pop("CODER_EVAL_RAW_SDK_LOG", None)
        msg = MagicMock(spec=[])
        agent = _make_agent()
        with caplog.at_level("INFO", logger="coder_eval.agents.claude_code_agent"):
            log_raw_sdk_event(agent._log, repr_target=msg, type="FakeMessage")
        assert "RAW_SDK_EVENT" not in caplog.text

    def test_enabled_by_env_var(self, caplog, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_RAW_SDK_LOG", "1")
        msg = MagicMock(spec=["some_attr"])
        msg.some_attr = "hello"
        agent = _make_agent()
        with caplog.at_level("INFO", logger="coder_eval.agents.claude_code_agent"):
            log_raw_sdk_event(agent._log, repr_target=msg, type="FakeMessage")
        assert "RAW_SDK_EVENT" in caplog.text
        assert "type=FakeMessage" in caplog.text
        assert "some_attr = 'hello'" in caplog.text

    def test_attr_target_overrides_dump_source(self, caplog, monkeypatch):
        """Codex passes a separate attr_target (the item root); it's dumped, not repr_target."""
        monkeypatch.setenv("CODER_EVAL_RAW_SDK_LOG", "1")
        notification = MagicMock(spec=["method"])
        notification.method = "item/started"
        root = MagicMock(spec=["type", "id"])
        root.type = "command_execution"
        root.id = "abc"
        agent = _make_agent()
        with caplog.at_level("INFO", logger="coder_eval.agents.claude_code_agent"):
            log_raw_sdk_event(
                agent._log,
                repr_target=notification,
                attr_target=root,
                method="item/started",
                root_type=root.type,
            )
        assert "method=item/started" in caplog.text
        assert "root_type=command_execution" in caplog.text
        # The dumped attrs come from `root`, not the notification.
        assert "id = 'abc'" in caplog.text


# --- _is_task_notification subtype fallback ---


def test_task_notification_subtype_string_fallback():
    """The subtype=="task_notification" path lets duck-typed mocks be recognized."""
    msg = MagicMock(spec=["subtype"])
    msg.subtype = "task_notification"
    assert _is_task_notification(msg) is True


def test_task_notification_wrong_subtype_not_matched():
    msg = MagicMock(spec=["subtype"])
    msg.subtype = "something_else"
    assert _is_task_notification(msg) is False
