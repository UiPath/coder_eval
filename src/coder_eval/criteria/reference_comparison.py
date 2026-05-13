"""Reference comparison criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, ReferenceComparisonCriterion
from coder_eval.scoring.complexity import ComplexityScorer
from coder_eval.scoring.similarity import SimilarityScorer


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class ReferenceComparisonChecker(BaseCriterion[ReferenceComparisonCriterion]):
    """Checker for ReferenceComparisonCriterion."""

    criterion_type = "reference_comparison"

    def _check_impl(
        self,
        criterion: ReferenceComparisonCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        route: "ApiRoute | None" = None,
    ) -> CriterionResult:
        """Compare agent code against reference solution.

        Uses the reference code passed to check_all() and compares it with
        the agent's generated file using the specified comparison method.

        Args:
            criterion: Reference comparison criterion
            sandbox: Sandbox instance for file access
            reference_code: Reference solution code for comparison

        Returns:
            Result with similarity score [0.0, 1.0]
        """
        # Check that reference code was provided
        if not reference_code:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="No reference code provided (task.reference not set)",
            )

        # Check sandbox is initialized
        if not sandbox.sandbox_dir:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="Sandbox not initialized",
            )

        # Load agent code
        agent_path = sandbox.sandbox_dir / criterion.agent_file
        if not agent_path.exists():
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Agent file not found: {criterion.agent_file}",
            )

        try:
            agent_code = agent_path.read_text(encoding="utf-8")
        except Exception as e:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Failed to read agent file: {e}",
            )

        # Compare using specified method
        try:
            similarity_scorer = SimilarityScorer()

            if criterion.comparison_method == "ast":
                score = similarity_scorer.score_ast_similarity(agent_code, reference_code)
            elif criterion.comparison_method == "token":
                score = similarity_scorer.score_token_similarity(agent_code, reference_code)
            elif criterion.comparison_method == "complexity":
                complexity_scorer = ComplexityScorer()
                ref_metrics = complexity_scorer.calculate_metrics(reference_code)
                reference_baseline = {
                    "cyclomatic": ref_metrics["cyclomatic_complexity"],
                    "lines_of_code": ref_metrics["lines_of_code"],
                    "function_count": ref_metrics["function_count"],
                }
                metrics = complexity_scorer.score_complexity(agent_code, reference_code, reference_baseline)
                score = metrics["scores"]["overall_complexity"]
            else:
                return CriterionResult(
                    criterion_type="reference_comparison",
                    description=criterion.description,
                    score=0.0,
                    error=f"Unknown comparison method: {criterion.comparison_method}",
                )

            details = (
                f"Comparison method: {criterion.comparison_method}\n"
                f"Similarity: {score:.3f}\n"
                f"Threshold: {criterion.similarity_threshold:.3f}"
            )

            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=score,
                details=details,
            )

        except Exception as e:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Comparison failed: {e}",
            )
