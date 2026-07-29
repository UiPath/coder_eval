"""Run-level derived metrics: one pass-rate denominator, and honest cost totals.

Two bugs are pinned here.

**The denominator.** ``pass_rate`` used to be ``succeeded / (run - error)``, which
paid a bonus for erroring: the more a run fell over, the smaller its denominator
got, up to the degenerate case of a run rendering as a perfect score while passing
a handful of rows. Every surface now divides by ``tasks_run``.

**The bill.** Cost was summed over whatever rows happened to carry one, so a run
whose model was missing from the rate card, or whose turns were killed before the
backend reported a cost, understated its spend silently. Unpriced spend is now
counted and the total is labelled a floor.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from coder_eval.models import FinalStatus, RunSummary
from coder_eval.pricing import is_priced, unpriced_models


def _row(status: FinalStatus | str, **extra: Any) -> dict[str, Any]:
    return {"task_id": f"t{id(extra)}", "status": status, **extra}


def _summary(rows: list[dict[str, Any]]) -> RunSummary:
    """Build a RunSummary whose stored counts are derived from ``rows``."""
    statuses = [FinalStatus(r["status"]) for r in rows]
    return RunSummary(
        run_id="2026-07-28_00-00-00",
        start_time=datetime(2026, 7, 28, 0, 0, 0),
        end_time=datetime(2026, 7, 28, 1, 0, 0),
        total_duration_seconds=3600.0,
        tasks_run=len(rows),
        tasks_succeeded=sum(1 for s in statuses if s.category == "succeeded"),
        tasks_failed=sum(1 for s in statuses if s.category == "failed"),
        tasks_error=sum(1 for s in statuses if s.category == "error"),
        task_results=rows,
        framework_version="0.0.0-test",
    )


class TestPassRateDenominator:
    def test_errors_count_as_misses(self):
        """The whole point: an error is in the denominator, not excluded from it."""
        summary = _summary(
            [
                _row(FinalStatus.SUCCESS),
                _row(FinalStatus.FAILURE),
                _row(FinalStatus.ERROR),
                _row(FinalStatus.ERROR),
            ]
        )
        assert summary.pass_rate == pytest.approx(0.25)
        # The old formula would have divided by 2 and published 50%.
        assert summary.error_share == pytest.approx(0.5)

    def test_build_failed_is_an_error_row(self):
        """BUILD_FAILED buckets to 'error' and so still lands in the denominator."""
        summary = _summary([_row(FinalStatus.SUCCESS), _row(FinalStatus.BUILD_FAILED)])
        assert summary.tasks_error == 1
        assert summary.pass_rate == pytest.approx(0.5)

    def test_timeout_and_budget_statuses_are_failures_not_errors(self):
        """These are task outcomes; they were always in the denominator and stay there."""
        summary = _summary(
            [
                _row(FinalStatus.SUCCESS),
                _row(FinalStatus.TIMEOUT),
                _row(FinalStatus.MAX_TURNS_EXHAUSTED),
                _row(FinalStatus.TOKEN_BUDGET_EXCEEDED),
                _row(FinalStatus.COST_BUDGET_EXCEEDED),
            ]
        )
        assert summary.tasks_error == 0
        assert summary.error_share == pytest.approx(0.0)
        assert summary.pass_rate == pytest.approx(0.2)

    def test_mostly_errored_run_cannot_render_as_perfect(self):
        """The reductio: 854 errors out of 861 rows.

        Under the old formula this reported 100.0% (7 evaluable, 7 passed). It must
        read as what it was.
        """
        rows = [_row(FinalStatus.SUCCESS) for _ in range(7)] + [_row(FinalStatus.ERROR) for _ in range(854)]
        summary = _summary(rows)
        assert summary.pass_rate == pytest.approx(7 / 861)
        assert summary.pass_rate < 0.01

    def test_empty_run_has_no_rate(self):
        """0/0 is unknown, not 0% — which would read as 'everything failed'."""
        summary = _summary([])
        assert summary.pass_rate is None
        assert summary.error_share is None

    def test_variant_aggregate_shares_the_denominator(self):
        """An A/B whose variants error at different rates must compare like with like."""
        from coder_eval.models import VariantAggregate

        agg = VariantAggregate(
            variant_id="v",
            tasks_run=4,
            tasks_succeeded=1,
            tasks_failed=1,
            tasks_error=2,
            average_score=0.5,
            average_duration=1.0,
        )
        assert agg.pass_rate == pytest.approx(0.25)

    def test_rates_serialize_into_run_json(self):
        """Downstream consumers must be able to READ the rate instead of re-deriving it.

        Independent re-derivations drift, and then two consumers publish different
        rates for the same run.
        """
        summary = _summary([_row(FinalStatus.SUCCESS), _row(FinalStatus.ERROR)])
        dumped = summary.model_dump()
        assert dumped["pass_rate"] == pytest.approx(0.5)
        assert dumped["error_share"] == pytest.approx(0.5)
        # And the round-trip tolerates its own output (computed fields aren't inputs).
        assert RunSummary.model_validate_json(summary.model_dump_json()).pass_rate == pytest.approx(0.5)


class TestCostCompleteness:
    def test_unpriced_row_is_counted_and_flagged(self):
        summary = _summary(
            [
                _row(FinalStatus.SUCCESS, total_tokens=1000, total_cost_usd=0.5, cost_complete=True),
                _row(FinalStatus.TIMEOUT, total_tokens=8_000_000, total_cost_usd=None, cost_complete=False),
            ]
        )
        assert summary.tasks_unpriced == 1
        assert summary.cost_complete is False
        # The priced row's cost still totals — a floor beats no number at all.
        assert summary.agent_cost_usd == pytest.approx(0.5)

    def test_partially_priced_row_is_counted(self):
        """A row whose cost is a partial sum of its own turns is incomplete.

        This is the timeout shape: turn 1 priced by the SDK, turn 2 killed
        mid-flight with real tokens and no cost. Summing turn 1 alone and calling
        it the row's cost is the silent 19% understatement.
        """
        summary = _summary(
            [_row(FinalStatus.TIMEOUT, total_tokens=5_000_000, total_cost_usd=1.25, cost_complete=False)]
        )
        assert summary.tasks_unpriced == 1
        assert summary.cost_complete is False

    def test_zero_token_error_is_not_unpriced(self):
        """A row that died before the agent ran genuinely cost nothing."""
        summary = _summary([_row(FinalStatus.ERROR, total_tokens=0, total_cost_usd=None, cost_complete=True)])
        assert summary.tasks_unpriced == 0
        assert summary.cost_complete is True
        assert summary.agent_cost_usd is None

    def test_eval_overhead_is_separate_from_the_agent_bill(self):
        """Judge/simulator spend is real money but must not inflate the agent's cost.

        It is a property of the suite's criteria and identical across harnesses, so
        folding it in would make two harnesses look closer than they are.
        """
        summary = _summary(
            [
                _row(
                    FinalStatus.SUCCESS,
                    total_tokens=1000,
                    total_cost_usd=1.0,
                    cost_complete=True,
                    judge_cost_usd=0.2,
                    simulator_cost_usd=0.05,
                ),
            ]
        )
        assert summary.agent_cost_usd == pytest.approx(1.0)
        assert summary.eval_overhead_cost_usd == pytest.approx(0.25)

    def test_no_overhead_reports_none(self):
        summary = _summary([_row(FinalStatus.SUCCESS, total_tokens=1, total_cost_usd=0.1, cost_complete=True)])
        assert summary.eval_overhead_cost_usd is None


class TestPricingCoverage:
    def test_is_priced_normalizes_bedrock_prefixes(self):
        assert is_priced("claude-sonnet-5")
        assert is_priced("eu.anthropic.claude-sonnet-5")
        assert not is_priced("claude-sonnet-99")

    def test_unpriced_models_dedupes_and_drops_empty(self):
        assert unpriced_models(["claude-sonnet-5", None, "", "made-up-1", "made-up-1", "made-up-0"]) == [
            "made-up-0",
            "made-up-1",
        ]


class TestRateCardCoversCheckedInExperiments:
    """CI guard: a model referenced by a committed experiment must be priced.

    The rate card is a static table baked into the installed version, so a model
    whose rates land in a later release prices as null on any turn the agent's own
    backend did not price. This class is what makes the next new model fail CI
    instead of a run's cost column.
    """

    @staticmethod
    def _models_in(doc: dict[str, Any]) -> set[str]:
        found: set[str] = set()
        for block in (doc.get("defaults") or {}, *(doc.get("variants") or [])):
            if not isinstance(block, dict):
                continue
            agent = block.get("agent")
            if isinstance(agent, dict) and agent.get("model"):
                found.add(str(agent["model"]))
        return found

    def test_every_experiment_model_is_priced(self):
        experiments_dir = Path(__file__).resolve().parents[1] / "experiments"
        referenced: dict[str, str] = {}
        for path in sorted(experiments_dir.glob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for model in self._models_in(doc):
                referenced.setdefault(model, path.name)

        # Guard the guard: an empty scan would pass vacuously.
        assert referenced, f"No agent.model found under {experiments_dir}; the discovery filter is broken."

        missing = {m: src for m, src in referenced.items() if not is_priced(m)}
        assert not missing, (
            "Models referenced by committed experiments have no pricing rate: "
            + ", ".join(f"{m!r} (from {src})" for m, src in sorted(missing.items()))
            + ". Add the rate to coder_eval.pricing._PRICING — an unpriced model records "
            + "null cost for every task and understates the run's bill silently."
        )
