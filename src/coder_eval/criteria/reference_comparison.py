"""Reference comparison criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.errors import CheckerMisuseError
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
        # Every failure below is a TASK-DEFINITION error, not an agent failure, so
        # they raise CheckerMisuseError (-> FinalStatus.ERROR) instead of returning
        # a gating score=0.0 (-> FinalStatus.FAILURE). A typo in `reference_file`
        # scored as 0.0 is counted against the agent's pass rate, and on a
        # dataset-fanned suite it silently zeroes every row and drags down the
        # CriterionAggregate mean, the suite_thresholds gate, the JUnit report and
        # the evalboard alike. The pre-directory-only equivalent raised out of
        # `load_reference`, so this restores the loud behaviour.
        ref_path = (reference_dir / criterion.reference_file).resolve()
        if not ref_path.is_relative_to(reference_dir.resolve()):
            raise CheckerMisuseError(
                f"reference_comparison.reference_file escapes the reference directory: {criterion.reference_file}"
            )
        try:
            reference_code = ref_path.read_text(encoding="utf-8")
        except OSError as e:
            raise CheckerMisuseError(
                f"reference_comparison.reference_file {criterion.reference_file!r} could not be read from the "
                + f"task's reference directory: {e}"
            ) from e
        if not reference_code:
            raise CheckerMisuseError(f"reference_comparison.reference_file is empty: {criterion.reference_file}")

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
            # CE035 exemption: genuinely the AGENT's failure, unlike reference_file
            # above: the task asked for this file and the agent did not produce
            # it, which is exactly what a gating 0.0 means.
            return CriterionResult(  # noqa: CE035
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
