"""Cost accounting on the error and timeout paths, where spend went missing.

Two seams:

1. **The pre-flight** (``check_pricing_coverage``). The rate card is a static
   table baked into the installed version, so a model released after it has no
   rate. It is the fallback for turns the agent's backend never priced, which
   makes killed and timed-out partials the ones that book their tokens against no
   money at all, with one log line to show for it.
2. **The row projection** (``eval_result_to_task_dict``) — judge and simulator
   spend was captured and rolled up nowhere, and a row whose turns were only
   partly priced reported its partial sum as the whole.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    ClaudeCodeAgentConfig,
    EvaluationResult,
    FinalStatus,
    JudgeCriterionResult,
    ResolvedTask,
    SimulationTelemetry,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestration.batch import check_pricing_coverage
from coder_eval.reports_experiment import eval_result_to_task_dict


def _resolved(model: str | None, tmp_path: Path) -> ResolvedTask:
    task = TaskDefinition(
        task_id="t1",
        description="d",
        initial_prompt="p",
        agent=ClaudeCodeAgentConfig(type="claude-code", model=model),
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
    )
    return ResolvedTask(
        task=task,
        task_file=tmp_path / "t1.yaml",
        run_dir=tmp_path / "runs" / "t1",
        variant_id="default",
    )


def _turn(iteration: int, usage: TokenUsage | None, model: str | None = None, crashed: bool = False) -> TurnRecord:
    return TurnRecord(
        iteration=iteration,
        user_input="p",
        agent_output="o",
        token_usage=usage,
        model_used=model,
        crashed=crashed,
    )


def _result(turns: list[TurnRecord], *, model: str | None = "claude-sonnet-5", **extra) -> EvaluationResult:
    return EvaluationResult(
        task_id="t1",
        task_description="d",
        agent_type="claude-code",
        model_used=model,
        started_at=datetime(2026, 7, 28, 0, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=len(turns),
        iterations=turns,
        **extra,
    )


class TestPricingPreflight:
    def test_priced_model_is_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            assert check_pricing_coverage([_resolved("claude-sonnet-5", tmp_path)]) == []
        assert "No pricing rate" not in caplog.text

    def test_unpriced_model_warns_but_runs(self, tmp_path, caplog):
        """Default is a loud warning, not a refusal: a brand-new model stays evaluable."""
        with caplog.at_level(logging.WARNING):
            missing = check_pricing_coverage([_resolved("claude-sonnet-99", tmp_path)])
        assert missing == ["claude-sonnet-99"]
        assert "No pricing rate" in caplog.text
        assert "understate the bill" in caplog.text

    def test_unpinned_model_is_not_flagged(self, tmp_path):
        """A task deferring its model to the route can't be pre-flighted from here."""
        assert check_pricing_coverage([_resolved(None, tmp_path)]) == []


class TestRowCostProjection:
    def test_cost_complete_false_when_a_turn_is_unpriced(self):
        result = _result(
            [
                _turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1)),
                _turn(2, TokenUsage(uncached_input_tokens=10, output_tokens=1)),
            ]
        )
        assert eval_result_to_task_dict(result)["cost_complete"] is False

    def test_cost_complete_true_when_every_burning_turn_is_priced(self):
        result = _result([_turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1))])
        assert eval_result_to_task_dict(result)["cost_complete"] is True

    def test_cost_complete_true_when_nothing_burned(self):
        """An error before the agent ran genuinely cost nothing — not 'missing cost'."""
        assert eval_result_to_task_dict(_result([]))["cost_complete"] is True
        assert eval_result_to_task_dict(_result([_turn(1, TokenUsage())]))["cost_complete"] is True

    def test_cost_complete_false_when_a_task_timeout_preserved_no_turn(self):
        """A task timeout with zero turns is unrecorded spend, not free.

        The watchdog SIGKILLs the agent by PID, so unlike a turn-level timeout this
        never reaches ``_on_attempt_failure`` and no partial turn is drained. The
        row lands with no turns, no tokens and no cost. Since a task timeout means
        the evaluation loop was still running, calling that row fully priced would
        be a false claim — the one case where cost is missing with no tokens to
        point at.
        """
        result = _result([])
        result.final_status = FinalStatus.TIMEOUT
        assert eval_result_to_task_dict(result)["cost_complete"] is False

    def test_cost_complete_true_for_a_fast_error_with_no_turn(self):
        """The companion: a setup failure has no turns either, and really is free.

        Keyed on status rather than elapsed time so a slow setup failure stays free
        while a task timeout never does.
        """
        result = _result([])
        result.final_status = FinalStatus.ERROR
        assert eval_result_to_task_dict(result)["cost_complete"] is True

    def test_judge_cost_rolls_up_onto_the_row(self):
        """Judge spend was captured per criterion and totalled nowhere."""
        result = _result([_turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1))])
        result.total_token_usage = TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1)
        result.success_criteria_results = [
            JudgeCriterionResult(
                criterion_type="llm_judge",
                description="d",
                score=1.0,
                token_usage=TokenUsage(uncached_input_tokens=5000, output_tokens=500, total_cost_usd=0.02),
            ),
            JudgeCriterionResult(
                criterion_type="llm_judge",
                description="d2",
                score=1.0,
                token_usage=TokenUsage(uncached_input_tokens=5000, output_tokens=500, total_cost_usd=0.03),
            ),
        ]
        row = eval_result_to_task_dict(result)
        assert row["judge_cost_usd"] == pytest.approx(0.05)
        # Kept out of the agent's bill.
        assert row["total_cost_usd"] == pytest.approx(0.1)

    def test_no_judge_means_no_judge_cost(self):
        """None, not 0.0 — 'no judge ran' must stay distinct from 'a judge ran free'."""
        result = _result([_turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1))])
        assert eval_result_to_task_dict(result)["judge_cost_usd"] is None

    @staticmethod
    def _simulated(**env) -> EvaluationResult:
        return _result(
            [_turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1))],
            simulation=SimulationTelemetry(
                n_trials=1,
                replicate_index=0,
                stop_reason="stop_token",
                simulator_input_tokens=1_000_000,
                simulator_output_tokens=100_000,
                total_turns=3,
            ),
            environment_info=env,
        )

    def test_simulator_prices_at_the_route_model_not_the_subject(self):
        """UserSimulator pins model=None, so it bills at BEDROCK_MODEL.

        A task that pins ``agent.model`` would otherwise mis-bill every simulated
        row. Here the subject is sonnet-5 ($3/$15) while the route is haiku-4.5
        ($1/$5), so the simulator must cost the haiku rate.
        """
        result = self._simulated(bedrock_model="claude-haiku-4-5-20251001")
        # 1M uncached input at $1/MTok + 100K output at $5/MTok.
        assert eval_result_to_task_dict(result)["simulator_cost_usd"] == pytest.approx(1.00 + 0.50)

    def test_simulator_falls_back_to_the_subject_model_off_bedrock(self):
        """A non-Bedrock route names no model on the record; the subject's is the best available."""
        result = self._simulated()
        # sonnet-5 at $3/$15 per MTok.
        assert eval_result_to_task_dict(result)["simulator_cost_usd"] == pytest.approx(3.0 + 1.5)

    def test_single_shot_row_has_no_simulator_cost(self):
        result = _result([_turn(1, TokenUsage(uncached_input_tokens=10, output_tokens=1, total_cost_usd=0.1))])
        assert eval_result_to_task_dict(result)["simulator_cost_usd"] is None


class TestErrorDiagnosticsOnTheRow:
    """Errors count as misses, so the rollup has to say why it lost those points.

    Without these, a run's zero-iteration errors cannot be characterised from
    run.json at all: every one needs its own task.json fetch.
    """

    def test_error_message_and_category_land_on_the_row(self):
        result = _result([], model=None)
        result.final_status = FinalStatus.ERROR
        result.error_message = "sandbox setup failed: no space left on device"
        result.error_details = {"error_category": "disk_full", "component": "orchestrator.setup"}

        row = eval_result_to_task_dict(result)
        assert row["error_message"] == "sandbox setup failed: no space left on device"
        assert row["error_category"] == "disk_full"

    def test_error_message_is_truncated(self):
        result = _result([], model=None)
        result.final_status = FinalStatus.ERROR
        result.error_message = "x" * 5000

        row = eval_result_to_task_dict(result)
        assert len(row["error_message"]) < 500
        assert row["error_message"].endswith("…")

    def test_clean_row_carries_no_error_fields(self):
        row = eval_result_to_task_dict(_result([]))
        assert row["error_message"] is None
        assert row["error_category"] is None
