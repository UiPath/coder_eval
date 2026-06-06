"""Tests for token usage tracking feature."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents.claude_code_agent import (
    ClaudeCodeAgent,
    _is_sdk_result_message,
    _is_task_notification,
)
from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    EvaluationResult,
    RunSummary,
    TokenUsage,
    TurnRecord,
    UserMessage,
    parse_agent_config,
)
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
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
            total_cost_usd=0.0123,
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cache_creation_input_tokens == 200
        assert usage.cache_read_input_tokens == 100
        assert usage.total_cost_usd == 0.0123

    def test_defaults_all_zeros(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.total_cost_usd is None

    def test_total_tokens_property(self):
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        assert usage.total_tokens == 1500

    def test_total_tokens_zero(self):
        usage = TokenUsage()
        assert usage.total_tokens == 0

    def test_serialization_roundtrip(self):
        original = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
            total_cost_usd=0.05,
        )
        json_str = original.model_dump_json()
        restored = TokenUsage.model_validate_json(json_str)

        assert restored.input_tokens == original.input_tokens
        assert restored.output_tokens == original.output_tokens
        assert restored.cache_creation_input_tokens == original.cache_creation_input_tokens
        assert restored.cache_read_input_tokens == original.cache_read_input_tokens
        assert restored.total_cost_usd == original.total_cost_usd

    def test_serialization_excludes_computed_property(self):
        usage = TokenUsage(input_tokens=100, output_tokens=50)
        dumped = usage.model_dump()
        # total_tokens is a @property, not a field, so it shouldn't be in dump
        assert "total_tokens" not in dumped


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
        usage = TokenUsage(input_tokens=500, output_tokens=200, total_cost_usd=0.01)
        record = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            token_usage=usage,
        )
        assert record.token_usage is not None
        assert record.token_usage.total_tokens == 700

    def test_turn_record_serialization_with_token_usage(self):
        usage = TokenUsage(input_tokens=500, output_tokens=200)
        record = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            token_usage=usage,
        )
        json_str = record.model_dump_json()
        restored = TurnRecord.model_validate_json(json_str)
        assert restored.token_usage is not None
        assert restored.token_usage.input_tokens == 500


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
            input_tokens=3000,
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
        assert usage.input_tokens == 18
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
        assert usage.input_tokens == 14
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
        assert usage.input_tokens == 1000
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
        assert usage.input_tokens == 1000
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
        assert usage.input_tokens == 10
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
        assert usage.input_tokens == 834
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
        assert usage.input_tokens == 105
        assert usage.output_tokens == 207
        assert usage.cache_creation_input_tokens == 9
        assert usage.total_cost_usd == pytest.approx(0.03)

    def test_model_usage_without_cost_falls_back_to_result_cost(self):
        model_usage = {"m": {"inputTokens": 10, "outputTokens": 20}}  # no costUSD
        usage = ClaudeCodeAgent._build_token_usage([], None, 0.5, model_usage)
        assert usage is not None
        assert usage.input_tokens == 10
        assert usage.total_cost_usd == 0.5

    def test_empty_model_usage_falls_through_to_stream(self):
        # Empty/absent model_usage must not shadow the stream-sum path.
        messages = [_assistant(message_id="m1", input_tokens=7, cache_read_tokens=300)]
        usage = ClaudeCodeAgent._build_token_usage(messages, None, None, {})
        assert usage is not None
        assert usage.input_tokens == 7
        assert usage.cache_read_input_tokens == 300


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
        assert record.token_usage.input_tokens == 1000
        assert record.token_usage.output_tokens == 500
        assert record.token_usage.cache_creation_input_tokens == 200
        assert record.token_usage.cache_read_input_tokens == 100
        assert record.token_usage.total_cost_usd == 0.0234
        assert record.token_usage.total_tokens == 1500

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
                token_usage=TokenUsage(input_tokens=1000, output_tokens=500, total_cost_usd=0.01),
            ),
            TurnRecord(
                iteration=2,
                user_input="p2",
                agent_output="r2",
                token_usage=TokenUsage(input_tokens=2000, output_tokens=800, total_cost_usd=0.02),
            ),
        ]

        turns_with_usage = [t for t in turns if t.token_usage]
        costs = [t.token_usage.total_cost_usd for t in turns_with_usage if t.token_usage.total_cost_usd is not None]
        aggregated = TokenUsage(
            input_tokens=sum(t.token_usage.input_tokens for t in turns_with_usage),
            output_tokens=sum(t.token_usage.output_tokens for t in turns_with_usage),
            cache_creation_input_tokens=sum(t.token_usage.cache_creation_input_tokens for t in turns_with_usage),
            cache_read_input_tokens=sum(t.token_usage.cache_read_input_tokens for t in turns_with_usage),
            total_cost_usd=sum(costs) if costs else None,
        )

        assert aggregated.input_tokens == 3000
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
                token_usage=TokenUsage(input_tokens=1000, output_tokens=500, total_cost_usd=0.01),
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
                token_usage=TokenUsage(input_tokens=800, output_tokens=400),
            ),
        ]

        turns_with_usage = [t for t in turns if t.token_usage]
        assert len(turns_with_usage) == 2

        costs = [t.token_usage.total_cost_usd for t in turns_with_usage if t.token_usage.total_cost_usd is not None]
        aggregated = TokenUsage(
            input_tokens=sum(t.token_usage.input_tokens for t in turns_with_usage),
            output_tokens=sum(t.token_usage.output_tokens for t in turns_with_usage),
            total_cost_usd=sum(costs) if costs else None,
        )

        assert aggregated.input_tokens == 1800
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
            },
        ]
        lines = ReportGenerator._generate_token_usage_section(task_results)
        joined = "\n".join(lines)

        assert "## Token Usage" in joined
        assert "**Total Tokens**: 5,000 (input: 3,000, output: 2,000)" in joined
        assert "**Total Cost**" not in joined  # No cost line when all costs are None
        assert "| task1 | 3,000 | 2,000 | 0 | 0 | 5,000 | N/A |" in joined

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


# --- _extract_sub_agent_usage tests ---


class TestExtractAgentUsage:
    """Tests for ClaudeCodeAgent._extract_sub_agent_usage (returns AgentUsage)."""

    def _make_msg(self, tool_use_result: dict | None, tool_use_id: str = "toolu_123") -> MagicMock:
        msg = MagicMock()
        msg.tool_use_result = tool_use_result
        block = MagicMock()
        block.tool_use_id = tool_use_id
        block.is_error = False
        msg.content = [block]
        return msg

    def test_extracts_full_breakdown(self):
        msg = self._make_msg(
            {
                "agentId": "agent-abc",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 1500,
                },
                "totalToolUseCount": 3,
                "totalDurationMs": 4200,
                "status": "completed",
            }
        )
        result = ClaudeCodeAgent._extract_sub_agent_usage(msg)
        assert result is not None
        # The full token breakdown is extracted onto the composed TokenUsage.
        assert result.tokens.input_tokens == 10
        assert result.tokens.output_tokens == 50
        assert result.tokens.cache_creation_input_tokens == 200
        assert result.tokens.cache_read_input_tokens == 1500
        # TokenUsage.total_tokens is input + output only (cache tokens excluded).
        assert result.tokens.total_tokens == 10 + 50
        assert result.tool_uses == 3

    def test_returns_none_for_non_agent_tool(self):
        # Bash/Read/Write results have no agentId
        msg = self._make_msg({"status": "completed", "output": "hello"})
        assert ClaudeCodeAgent._extract_sub_agent_usage(msg) is None

    def test_returns_none_when_tool_use_result_missing(self):
        msg = MagicMock()
        msg.tool_use_result = None
        assert ClaudeCodeAgent._extract_sub_agent_usage(msg) is None

    def test_returns_none_when_usage_missing(self):
        msg = self._make_msg({"agentId": "agent-abc"})  # no usage key
        assert ClaudeCodeAgent._extract_sub_agent_usage(msg) is None

    def test_coerces_missing_token_fields_to_zero(self):
        msg = self._make_msg(
            {
                "agentId": "agent-abc",
                "usage": {},  # all fields absent
                "totalToolUseCount": 1,
                "totalDurationMs": 100,
                "status": "completed",
            }
        )
        result = ClaudeCodeAgent._extract_sub_agent_usage(msg)
        assert result is not None
        assert result.tokens.input_tokens == 0
        assert result.tokens.output_tokens == 0
        assert result.tokens.cache_creation_input_tokens == 0
        assert result.tokens.cache_read_input_tokens == 0
        assert result.tokens.total_tokens == 0

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
                "totalToolUseCount": 0,
                "totalDurationMs": 0,
                "status": "completed",
            }
        )
        result = ClaudeCodeAgent._extract_sub_agent_usage(msg)
        assert result is not None
        assert result.tokens.total_tokens == 0

    def test_extracts_usage_when_tool_result_block_present(self):
        # The spawning Agent tool_use_id is read off the ToolResultBlock for
        # potential logging, but no longer surfaces on AgentUsage (attribution
        # now comes from the event tree's parent_thread_id). Presence of the
        # block must not interfere with token extraction.
        msg = self._make_msg(
            {"agentId": "agent-xyz", "usage": {"output_tokens": 5}, "status": "completed"},
            tool_use_id="toolu_SPECIFIC",
        )
        result = ClaudeCodeAgent._extract_sub_agent_usage(msg)
        assert result is not None
        assert result.tokens.output_tokens == 5
        # tool_use_id is intentionally not exposed on the returned AgentUsage.
        assert not hasattr(result, "tool_use_id")

    def test_extracts_usage_when_no_content_block(self):
        # With no ToolResultBlock to read a tool_use_id from, extraction still
        # succeeds and returns a valid AgentUsage.
        msg = MagicMock()
        msg.tool_use_result = {"agentId": "agent-abc", "usage": {"output_tokens": 5}, "status": "completed"}
        msg.content = []
        result = ClaudeCodeAgent._extract_sub_agent_usage(msg)
        assert result is not None
        assert result.tokens.output_tokens == 5


# --- _log_message_raw env-gate tests ---


def _make_agent() -> ClaudeCodeAgent:
    return ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))


class TestLogMessageRaw:
    def test_disabled_by_default(self, caplog):
        import os

        os.environ.pop("CODER_EVAL_RAW_SDK_LOG", None)
        msg = MagicMock(spec=[])
        agent = _make_agent()
        with caplog.at_level("INFO", logger="coder_eval.agents.claude_code_agent"):
            agent._log_message_raw(msg, "FakeMessage")
        assert "RAW_SDK_EVENT" not in caplog.text

    def test_enabled_by_env_var(self, caplog, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_RAW_SDK_LOG", "1")
        msg = MagicMock(spec=["some_attr"])
        msg.some_attr = "hello"
        agent = _make_agent()
        with caplog.at_level("INFO", logger="coder_eval.agents.claude_code_agent"):
            agent._log_message_raw(msg, "FakeMessage")
        assert "RAW_SDK_EVENT" in caplog.text
        assert "FakeMessage" in caplog.text


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
