"""Tests for token usage tracking feature."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _is_sdk_result_message
from coder_eval.models import AgentConfig, AgentKind, EvaluationResult, RunSummary, TokenUsage, TurnRecord
from coder_eval.reports import ReportGenerator


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
        msg = MagicMock()
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


# --- Agent token capture tests ---


class TestAgentTokenCapture:
    """Tests for token usage capture in ClaudeCodeAgent.communicate()."""

    @pytest.mark.asyncio
    async def test_communicate_captures_token_usage(self):
        """Verify that when SDK yields a ResultMessage with usage, TurnRecord.token_usage is populated."""
        config = AgentConfig(
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
        config = AgentConfig(
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
        config = AgentConfig(
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
