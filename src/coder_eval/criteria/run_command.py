"""Run command criterion checker."""

import logging
import math
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, RunCommandCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


# Per-stream output budget included in criterion details. Large enough for
# typical tool diagnostics (e.g. uip --output json, pytest tracebacks) while
# preventing a runaway process from blowing up the JSON trace or HTML report.
_OUTPUT_BUDGET_CHARS = 4000


def _format_stream(label: str, text: str) -> str:
    """Render a stream (stdout or stderr) with clear truncation markers.

    Always emits a header so an empty stream reads as "(empty)" rather than
    being silently dropped, making it obvious that the checker actually
    captured the stream instead of missing it.
    """
    if not text:
        return f"{label}: (empty)"
    stripped = text
    if len(stripped) > _OUTPUT_BUDGET_CHARS:
        dropped = len(stripped) - _OUTPUT_BUDGET_CHARS
        stripped = stripped[:_OUTPUT_BUDGET_CHARS] + f"\n… ({dropped} more chars truncated)"
    return f"{label}:\n{stripped}"


def _build_exec_details(command: str, exit_code: int, expected_exit: int, stdout: str, stderr: str) -> str:
    """Build a stable details block covering command, exit, and both streams."""
    return (
        f"Command: {command}\n"
        f"Exit code: {exit_code} (expected: {expected_exit})\n"
        f"{_format_stream('Stdout', stdout)}\n"
        f"{_format_stream('Stderr', stderr)}"
    )


@register_criterion
class RunCommandChecker(BaseCriterion[RunCommandCriterion]):
    """Checker for RunCommandCriterion."""

    criterion_type = "run_command"

    def _check_impl(
        self,
        criterion: RunCommandCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Execute command and dispatch to the appropriate scoring method."""
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, stderr = sandbox.run_command(criterion.command, timeout=criterion.timeout)

        if criterion.score_from_stdout:
            return self._score_from_stdout(criterion, exit_code, stdout, stderr)
        return self._binary_check(criterion, exit_code, stdout, stderr)

    def _binary_check(
        self,
        criterion: RunCommandCriterion,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> CriterionResult:
        """Binary pass/fail on exit code, with optional stdout matching."""
        exit_ok = exit_code == criterion.expected_exit_code

        details = _build_exec_details(
            criterion.command,
            exit_code,
            criterion.expected_exit_code,
            stdout,
            stderr,
        )

        stdout_ok = True
        if criterion.expected_stdout is not None:
            stdout_ok = self._match_stdout(stdout, criterion.expected_stdout, criterion.stdout_match)
            details += f"\nStdout match ({criterion.stdout_match}): {'pass' if stdout_ok else 'FAIL'}"
            if not stdout_ok:
                details += f"\nExpected: {criterion.expected_stdout[:200]}"

        score = 1.0 if (exit_ok and stdout_ok) else 0.0

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    def _score_from_stdout(
        self,
        criterion: RunCommandCriterion,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> CriterionResult:
        """Read a float score (0.0-1.0) from the first line of stdout."""
        if exit_code != criterion.expected_exit_code:
            details = _build_exec_details(
                criterion.command,
                exit_code,
                criterion.expected_exit_code,
                stdout,
                stderr,
            )
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=details,
                error=f"Command exited with code {exit_code} (expected {criterion.expected_exit_code})",
            )

        lines = stdout.splitlines()
        if not lines or not lines[0].strip():
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=_build_exec_details(
                    criterion.command,
                    exit_code,
                    criterion.expected_exit_code,
                    stdout,
                    stderr,
                ),
                error="No output from command (empty stdout)",
            )

        first_line = lines[0].strip()
        try:
            raw_score = float(first_line)
        except ValueError:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=_build_exec_details(
                    criterion.command,
                    exit_code,
                    criterion.expected_exit_code,
                    stdout,
                    stderr,
                ),
                error=f"Could not parse score from first line: {first_line!r}",
            )

        if math.isnan(raw_score) or math.isinf(raw_score):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=_build_exec_details(
                    criterion.command,
                    exit_code,
                    criterion.expected_exit_code,
                    stdout,
                    stderr,
                ),
                error=f"Invalid score value from stdout: {first_line!r}",
            )

        score = max(0.0, min(1.0, raw_score))
        details_header = f"Score: {score:.3f}"
        if score != raw_score:
            details_header += f" (clamped from {raw_score:.3f})"

        exec_details = _build_exec_details(
            criterion.command,
            exit_code,
            criterion.expected_exit_code,
            stdout,
            stderr,
        )
        details = f"{details_header}\n{exec_details}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @staticmethod
    def _match_stdout(actual: str, expected: str, mode: str) -> bool:
        """Compare stdout against expected value using the given mode.

        Only ``exact``/``contains`` strip both sides — ``regex`` passes the
        pattern verbatim so authored whitespace (e.g. ``^   indented$``)
        reaches ``re.search`` intact.
        """
        if mode == "exact":
            return actual.strip() == expected.strip()
        if mode == "contains":
            return expected.strip() in actual.strip()
        if mode == "regex":
            try:
                return re.search(expected, actual) is not None
            except re.error as e:
                logger.warning("Invalid stdout regex pattern '%s': %s", expected, e)
                return False
        raise ValueError(f"Unknown stdout_match mode: {mode!r}")
