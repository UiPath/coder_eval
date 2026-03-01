"""Tests for assistant turn counting metric.

Covers:
- TurnRecord.assistant_turn_count field behavior
- EvaluationResult.total_assistant_turns aggregation
- Report generation with Asst Turns column
- Edge cases (zero turns, backward compatibility)

Agent-level tests (require claude_agent_sdk) are in test_agent_telemetry.py.
"""

from datetime import datetime

from coder_eval.models import AgentKind, EvaluationResult, RunSummary, TurnRecord
from coder_eval.reports import ReportGenerator


# --- Model-level tests ---


class TestTurnRecordAssistantTurnCount:
    """Tests for TurnRecord.assistant_turn_count field."""

    def test_default_value(self):
        """Default assistant_turn_count should be 0."""
        turn = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
        )
        assert turn.assistant_turn_count == 0

    def test_explicit_value(self):
        """Explicit assistant_turn_count should be preserved."""
        turn = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            assistant_turn_count=5,
        )
        assert turn.assistant_turn_count == 5

    def test_serialization_roundtrip(self):
        """assistant_turn_count should survive JSON serialization."""
        turn = TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="response",
            assistant_turn_count=7,
        )
        json_str = turn.model_dump_json()
        restored = TurnRecord.model_validate_json(json_str)
        assert restored.assistant_turn_count == 7

    def test_backward_compatible_deserialization(self):
        """Old JSON without assistant_turn_count should deserialize with default 0."""
        old_json = '{"iteration": 1, "user_input": "test", "agent_output": "out"}'
        turn = TurnRecord.model_validate_json(old_json)
        assert turn.assistant_turn_count == 0


class TestEvaluationResultTotalAssistantTurns:
    """Tests for EvaluationResult.total_assistant_turns aggregation."""

    def test_default_is_none(self):
        """total_assistant_turns should default to None."""
        result = EvaluationResult(
            task_id="test",
            task_description="desc",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=0,
        )
        assert result.total_assistant_turns is None

    def test_aggregation_from_turns(self):
        """total_assistant_turns should be the sum of all turn counts."""
        result = EvaluationResult(
            task_id="test",
            task_description="desc",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=3,
            turns=[
                TurnRecord(iteration=1, user_input="a", agent_output="b", assistant_turn_count=3),
                TurnRecord(iteration=2, user_input="c", agent_output="d", assistant_turn_count=5),
                TurnRecord(iteration=3, user_input="e", agent_output="f", assistant_turn_count=2),
            ],
        )
        # Simulate what orchestrator does
        result.total_assistant_turns = sum(t.assistant_turn_count for t in result.turns)
        assert result.total_assistant_turns == 10

    def test_no_turns_stays_none(self):
        """When there are no turns, total_assistant_turns should remain None.

        This tests the guard that the orchestrator has:
            if self.result.turns:
                self.result.total_assistant_turns = sum(...)
        """
        result = EvaluationResult(
            task_id="test",
            task_description="desc",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="ERROR",
            iteration_count=0,
            turns=[],
        )
        # Simulate orchestrator guard
        if result.turns:
            result.total_assistant_turns = sum(t.assistant_turn_count for t in result.turns)

        assert result.total_assistant_turns is None

    def test_serialization_roundtrip(self):
        """total_assistant_turns should survive JSON roundtrip."""
        result = EvaluationResult(
            task_id="test",
            task_description="desc",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            total_assistant_turns=9,
        )
        json_str = result.model_dump_json()
        restored = EvaluationResult.model_validate_json(json_str)
        assert restored.total_assistant_turns == 9

    def test_backward_compatible_deserialization(self):
        """Old JSON without total_assistant_turns should deserialize with None default."""
        old_json = (
            '{"task_id": "t", "task_description": "d", "agent_type": "claude-code",'
            '"started_at": "2025-01-01T00:00:00", "final_status": "SUCCESS", "iteration_count": 1}'
        )
        result = EvaluationResult.model_validate_json(old_json)
        assert result.total_assistant_turns is None


# --- Report-level tests ---


def _make_task_result(task_id, turns=None, **kwargs):
    """Helper to create a task result dict."""
    return {
        "task_id": task_id,
        "status": kwargs.get("status", "SUCCESS"),
        "weighted_score": kwargs.get("weighted_score", 1.0),
        "duration": kwargs.get("duration", 60.0),
        "iteration_count": kwargs.get("iteration_count", 1),
        "turns": turns or [],
    }


class TestReportAssistantTurns:
    """Tests for Asst Turns column in Generation Metrics report."""

    def test_asst_turns_column_present(self):
        """Generation Metrics table should have Asst Turns column header."""
        task_results = [
            _make_task_result(
                "task1",
                turns=[{"iteration": 1, "duration_seconds": 30.0, "assistant_turn_count": 5}],
            ),
        ]
        lines = ReportGenerator._generate_generation_metrics_section(task_results)
        header = lines[2]  # Third line is the header row
        assert "Asst Turns" in header

    def test_asst_turns_values_rendered(self):
        """Asst Turns column should show summed assistant_turn_count per task."""
        task_results = [
            _make_task_result(
                "task1",
                turns=[
                    {"iteration": 1, "duration_seconds": 20.0, "assistant_turn_count": 3},
                    {"iteration": 2, "duration_seconds": 25.0, "assistant_turn_count": 5},
                ],
                duration=45.0,
                iteration_count=2,
            ),
        ]
        lines = ReportGenerator._generate_generation_metrics_section(task_results)
        data_rows = [line for line in lines if line.startswith("| task1")]
        assert len(data_rows) == 1
        # Asst turns for task1 = 3 + 5 = 8
        assert "| 8 |" in data_rows[0]

    def test_asst_turns_defaults_to_zero_for_legacy_data(self):
        """Legacy turn data without assistant_turn_count should show 0."""
        task_results = [
            _make_task_result(
                "task1",
                turns=[{"iteration": 1, "duration_seconds": 30.0}],
            ),
        ]
        lines = ReportGenerator._generate_generation_metrics_section(task_results)
        data_rows = [line for line in lines if line.startswith("| task1")]
        assert len(data_rows) == 1
        assert "| 0 |" in data_rows[0]

    def test_total_assistant_turns_in_summary(self):
        """Run summary should include Total Assistant Turns when > 0."""
        summary = RunSummary(
            run_id="test",
            start_time=datetime(2025, 1, 1),
            end_time=datetime(2025, 1, 1, 0, 1),
            total_duration_seconds=60.0,
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            task_results=[
                _make_task_result(
                    "task1",
                    turns=[
                        {"iteration": 1, "duration_seconds": 30.0, "assistant_turn_count": 4},
                        {"iteration": 2, "duration_seconds": 30.0, "assistant_turn_count": 6},
                    ],
                    iteration_count=2,
                ),
            ],
            framework_version="0.1.0",
            environment_info={},
        )
        report = ReportGenerator.generate_markdown(summary)
        assert "Total Assistant Turns**: 10" in report

    def test_no_total_assistant_turns_when_zero(self):
        """Run summary should NOT include Total Assistant Turns when all are 0."""
        summary = RunSummary(
            run_id="test",
            start_time=datetime(2025, 1, 1),
            end_time=datetime(2025, 1, 1, 0, 1),
            total_duration_seconds=60.0,
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            task_results=[
                _make_task_result(
                    "task1",
                    turns=[{"iteration": 1, "duration_seconds": 30.0}],
                    iteration_count=1,
                ),
            ],
            framework_version="0.1.0",
            environment_info={},
        )
        report = ReportGenerator.generate_markdown(summary)
        assert "Total Assistant Turns" not in report


class TestRunSummaryAssistantTurnCount:
    """Tests for assistant_turn_count propagation through run summary generation."""

    def test_summary_includes_assistant_turn_count(self, tmp_path):
        """Verify _generate_run_summary includes assistant_turn_count in turns data."""
        from coder_eval.orchestration.batch import _generate_run_summary

        result = EvaluationResult(
            task_id="task1",
            task_description="Test",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=2,
            turns=[
                TurnRecord(iteration=1, user_input="a", agent_output="b", assistant_turn_count=3),
                TurnRecord(iteration=2, user_input="c", agent_output="d", assistant_turn_count=7),
            ],
        )

        summary = _generate_run_summary(
            run_dir=tmp_path,
            task_results=[{"task_id": "task1", "result": result, "duration": 10.0}],
            start_time=datetime.now(),
            end_time=datetime.now(),
        )

        # Verify turns in summary contain assistant_turn_count
        task_turns = summary.task_results[0]["turns"]
        assert task_turns[0]["assistant_turn_count"] == 3
        assert task_turns[1]["assistant_turn_count"] == 7

    def test_summary_report_renders_assistant_turns(self, tmp_path):
        """End-to-end: summary generation -> report rendering shows correct Asst Turns."""
        from coder_eval.orchestration.batch import _generate_run_summary

        result = EvaluationResult(
            task_id="task1",
            task_description="Test",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            turns=[
                TurnRecord(iteration=1, user_input="a", agent_output="b", assistant_turn_count=5),
            ],
        )

        summary = _generate_run_summary(
            run_dir=tmp_path,
            task_results=[{"task_id": "task1", "result": result, "duration": 10.0}],
            start_time=datetime.now(),
            end_time=datetime.now(),
        )

        report = ReportGenerator.generate_markdown(summary)
        assert "| 5 |" in report
        assert "Total Assistant Turns**: 5" in report
