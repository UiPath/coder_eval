"""Reference comparison criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, ReferenceComparisonCriterion
from coder_eval.scoring.complexity import ComplexityScorer
from coder_eval.scoring.similarity import SimilarityScorer


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
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
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Compare agent code against one file inside the reference directory.

        The reference is always a directory (``task.reference.directory``);
        ``criterion.reference_file`` names the single file within it to compare
        against, mirroring how judges address reference assets with
        ``$REFERENCE_DIR/<path>``.

        Args:
            criterion: Reference comparison criterion
            sandbox: Sandbox instance for file access
            turn_records: Unused for this criterion
            context: Carries ``reference_dir`` (the per-run staged reference copy)

        Returns:
            Result with similarity score [0.0, 1.0]
        """
        reference_dir = context.reference_dir if context else None
        if reference_dir is None:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="No reference directory provided (task.reference not set)",
            )

        # Confined to the reference dir on purpose: unlike a judge's `files:`
        # entry (author-written, trusted, and deliberately allowed to escape via
        # `$REFERENCE_DIR/../shared/...`), this field names one file *of the
        # solution being compared against*, so traversal out of the staged copy
        # is always a mistake.
        ref_path = (reference_dir / criterion.reference_file).resolve()
        if not ref_path.is_relative_to(reference_dir.resolve()):
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"reference_file escapes the reference directory: {criterion.reference_file}",
            )
        try:
            reference_code = ref_path.read_text(encoding="utf-8")
        except OSError as e:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Failed to read reference file {criterion.reference_file}: {e}",
            )
        if not reference_code:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Reference file is empty: {criterion.reference_file}",
            )

        # Check sandbox is initialized
        if not sandbox.sandbox_dir:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="Sandbox not initialized",
            )

        # Load agent code through the shared path seam, so `agent_file` resolves
        # (glob expansion, ignore filtering, exactly-one) like every other
        # sandbox-relative criterion path.
        try:
            agent_code = sandbox.get_file_content(criterion.agent_file)
        except FileNotFoundError:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Agent file not found: {criterion.agent_file}",
            )
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
