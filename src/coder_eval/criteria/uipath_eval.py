"""UiPath eval criterion checker."""

import json
import logging
import re
import shlex
import uuid
from typing import TYPE_CHECKING, Any

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, UiPathEvalCriterion


# Detector for "the `uipath` CLI is not available in the sandbox". Sandboxes run
# on Linux (/bin/sh — dash on Ubuntu), so exit code 127 + "command not found" is
# the only shell signal we need to recognize. The regex also matches the python
# ModuleNotFoundError format for tasks that run `python -m uipath`.
_UIPATH_MISSING_PATTERN = re.compile(
    r"\buipath\b.*command not found|no module named ['\"]?uipath['\"]?(?:\s|$)",
    re.IGNORECASE,
)


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class UiPathEvalChecker(BaseCriterion[UiPathEvalCriterion]):
    """Checker for UiPathEvalCriterion."""

    criterion_type = "uipath_eval"

    def _check_impl(
        self,
        criterion: UiPathEvalCriterion,
        sandbox: "Sandbox",
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Check UiPath agent evaluation results against specified thresholds.

        Args:
            criterion: UiPathEvalCriterion with agent_name, eval_set, and thresholds
            sandbox: Sandbox instance for file access and command execution
            turn_records: Not used for this criterion

        Returns:
            CriterionResult with score (0.0-1.0) based on threshold compliance
        """
        # Resolve eval_set path relative to task directory
        eval_set_path = criterion.eval_set
        task_dir = sandbox.task_dir
        if task_dir:
            resolved_path = (task_dir / criterion.eval_set).resolve()
            if resolved_path.exists():
                eval_set_path = str(resolved_path)

        # Use a unique output filename to avoid conflicts
        output_filename = f"uipath_eval_output_{uuid.uuid4().hex[:8]}.json"

        # Run the evaluation command with properly escaped arguments
        cmd = (
            f"uv run uipath eval {shlex.quote(criterion.agent_name)} "
            f"{shlex.quote(eval_set_path)} --no-report --output-file {shlex.quote(output_filename)}"
        )
        exit_code, _, stderr = sandbox.run_command(cmd)

        if exit_code != 0:
            stderr_text = stderr or ""
            # Exit code 127 is the POSIX "command not found" signal; the regex
            # also catches `ModuleNotFoundError` for tasks that invoke
            # `python -m uipath` instead of the CLI.
            cli_missing = exit_code == 127 or bool(_UIPATH_MISSING_PATTERN.search(stderr_text))
            hint = ""
            if cli_missing:
                # The host's `[uipath]` extra installs `uipath` into the host
                # venv; the sandbox runs in its own `uv` environment and must
                # resolve `uipath` from the task's own deps.
                hint = (
                    " — the sandbox could not resolve the `uipath` CLI. "
                    "Ensure the task's Python deps include `uipath` (the in-sandbox "
                    "dependency is separate from the host's `coder-eval[uipath]` extra)."
                )
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=f"Command failed with exit code {exit_code}: {stderr}{hint}",
            )

        # Parse the output JSON file
        try:
            output_content = sandbox.get_file_content(output_filename)
            results = json.loads(output_content)
        except Exception as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=f"Failed to parse {output_filename}: {e}",
            )

        if not isinstance(results, dict):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=f"Expected JSON object in {output_filename}, got {type(results).__name__}",
            )

        # Calculate metrics from evaluation results
        metrics = self._calculate_metrics(results)

        # Compare metrics against thresholds
        failed_metrics = []
        for metric_name, threshold in criterion.thresholds.items():
            if metric_name not in metrics:
                failed_metrics.append(f"Missing metric: {metric_name}")
                continue

            metric_value = metrics[metric_name]
            if not isinstance(metric_value, (int, float)):
                failed_metrics.append(f"{metric_name}: Invalid type {type(metric_value).__name__} (expected number)")
            elif metric_value < threshold:
                failed_metrics.append(f"{metric_name}: {metric_value} < {threshold}")

        # Calculate score based on how many thresholds are met
        if not criterion.thresholds:
            score = 1.0
        else:
            metrics_passed = len(criterion.thresholds) - len(failed_metrics)
            score = metrics_passed / len(criterion.thresholds)

        details = "All thresholds met." if not failed_metrics else f"Failed metrics: {', '.join(failed_metrics)}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    def _calculate_metrics(self, results: Any) -> dict[str, float]:
        """Calculate aggregated metrics from evaluation results.

        Supports two formats:
        1. Complex format with evaluationSetResults (from UiPath agents)
        2. Simple format with flat metric keys (for testing/direct evaluation)

        Args:
            results: Parsed output.json with either evaluationSetResults or flat metrics

        Returns:
            Dictionary with calculated metrics
        """
        # Try complex format first (evaluationSetResults)
        evaluation_set_results = results.get("evaluationSetResults", [])
        if evaluation_set_results:
            return self._calculate_metrics_from_evaluations(evaluation_set_results)

        # Fall back to simple format (flat metrics)
        # Keep all values including non-numeric ones, to catch type errors later
        metrics = dict(results)

        logger.debug(f"Calculated metrics: {metrics}")
        return metrics

    def _calculate_metrics_from_evaluations(self, evaluation_set_results: Any) -> dict[str, float]:
        """Calculate metrics from evaluationSetResults structure.

        Groups metrics by evaluator ID, averages scores per evaluator (treating
        exceptions as 0.0), then computes macro average across all evaluators.

        Args:
            evaluation_set_results: List of evaluation results with evaluatorId

        Returns:
            Dictionary with calculated metrics, keyed by evaluator ID or "*" for macro average
        """
        evaluator_scores: dict[str, list[float]] = {}  # evaluator_id -> list of scores

        # Iterate through all evaluations and collect scores by evaluator
        for evaluation in evaluation_set_results:
            evaluation_run_results = evaluation.get("evaluationRunResults", [])
            for run_result in evaluation_run_results:
                evaluator_id = run_result.get("evaluatorId")
                if not evaluator_id:
                    continue

                result_data = run_result.get("result", {})
                score = result_data.get("score", 0.0)

                # Treat missing/error/non-numeric scores as 0.0
                if not isinstance(score, (int, float)):
                    score = 0.0

                if evaluator_id not in evaluator_scores:
                    evaluator_scores[evaluator_id] = []
                evaluator_scores[evaluator_id].append(score)

        # Calculate per-evaluator averages
        metrics = {}
        evaluator_averages = []
        for evaluator_id, scores in evaluator_scores.items():
            avg_score = sum(scores) / len(scores) if scores else 0.0
            metrics[evaluator_id] = avg_score
            evaluator_averages.append(avg_score)

        # Calculate macro average (average of evaluator averages)
        if evaluator_averages:
            metrics["*"] = sum(evaluator_averages) / len(evaluator_averages)
        else:
            metrics["*"] = 0.0

        logger.debug(f"Calculated metrics: {metrics}")
        return metrics
