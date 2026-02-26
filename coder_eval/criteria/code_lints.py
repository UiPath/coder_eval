"""Code lints criterion checker."""

import logging
import shlex
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CodeLintsCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class CodeLintsChecker(BaseCriterion[CodeLintsCriterion]):
    """Checker for CodeLintsCriterion."""

    criterion_type = "code_lints"

    def _check_impl(
        self,
        criterion: CodeLintsCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Run linter and check results.

        Args:
            criterion: Code lints criterion
            sandbox: Sandbox instance for command execution
            reference_code: Not used for this criterion

        Returns:
            Result with binary score (1.0 if linter passes, 0.0 if errors/warnings)
        """
        # Build linter command with proper quoting for paths/args with spaces
        # Parse linter safely to handle multi-word commands (e.g., "ruff check")
        try:
            linter_parts = shlex.split(criterion.linter)
        except ValueError as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=f"Invalid linter command: {e}",
                error=f"Failed to parse linter command: {e}",
            )

        # Build full command with quoted path and args
        # CRITICAL: Re-quote linter parts to neutralize shell metacharacters
        cmd_parts = [shlex.quote(part) for part in linter_parts]
        cmd_parts.append(shlex.quote(criterion.path))
        cmd_parts.extend(shlex.quote(arg) for arg in criterion.args)
        command = " ".join(cmd_parts)
        logger.debug(f"Running linter for criterion '{criterion.description}': {command}")

        exit_code, stdout, stderr = sandbox.run_command(command, timeout=criterion.timeout)

        # Most linters return 0 on success, non-zero on issues
        # Some linters (like ruff) return different codes for errors vs warnings
        # Calculate score: allow_warnings accepts 0/1, strict mode only accepts 0
        score = (1.0 if exit_code in (0, 1) else 0.0) if criterion.allow_warnings else (1.0 if exit_code == 0 else 0.0)

        # Build details from output
        details = f"Exit code: {exit_code} (score: {score:.2f})\n"

        # Include relevant output (truncate if too long)
        output = stdout if stdout else stderr
        if output:
            lines = output.strip().split("\n")
            if len(lines) <= 10:
                details += output
            else:
                # Show first few and last few lines
                details += "\n".join(lines[:5])
                details += f"\n... ({len(lines) - 10} more lines) ...\n"
                details += "\n".join(lines[-5:])
        else:
            details += "No linter output (clean)"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
