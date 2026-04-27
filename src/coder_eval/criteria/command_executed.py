"""Command executed criterion checker."""

import json
import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CommandExecutedCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# Limit regex search input length to mitigate ReDoS on large command strings
_MAX_PATTERN_SEARCH_LEN = 2000


@register_criterion
class CommandExecutedChecker(BaseCriterion[CommandExecutedCriterion]):
    """Checker for CommandExecutedCriterion.

    Inspects agent CommandTelemetry records to verify specific
    tools/commands were used during evaluation.
    """

    criterion_type = "command_executed"

    def _check_impl(
        self,
        criterion: CommandExecutedCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Check if the agent executed commands matching the criterion filters.

        Scoring is fractional: min(1.0, matching_count / min_count).

        Args:
            criterion: Command executed criterion with filters
            sandbox: Sandbox instance (not used for this criterion)
            reference_code: Not used for this criterion
            turn_records: List of turn records containing command telemetry

        Returns:
            CriterionResult with fractional score based on matching commands
        """
        if turn_records is None:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details="No turn records available",
                error="turn_records not provided to checker",
            )

        # Collect all commands from all turns
        all_commands = [cmd for turn in turn_records for cmd in turn.commands]

        if not all_commands:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details="No commands found in turn records",
            )

        # Compile regex patterns if provided.
        # re.DOTALL so `.` matches newlines — agents commonly write multi-line bash
        # commands using backslash line-continuation (e.g. `uip ... \\\n  --body ...`),
        # and patterns like `foo.*--body` must span those line breaks.
        pattern: re.Pattern[str] | None = None
        if criterion.command_pattern is not None:
            try:
                pattern = re.compile(criterion.command_pattern, re.DOTALL)
            except re.error as e:
                return CriterionResult(
                    criterion_type=criterion.type,
                    description=criterion.description,
                    score=0.0,
                    details=f"Invalid command_pattern regex: {e}",
                    error=f"Invalid regex: {e}",
                )

        exclude_re: re.Pattern[str] | None = None
        if criterion.exclude_pattern is not None:
            try:
                exclude_re = re.compile(criterion.exclude_pattern, re.DOTALL)
            except re.error as e:
                return CriterionResult(
                    criterion_type=criterion.type,
                    description=criterion.description,
                    score=0.0,
                    details=f"Invalid exclude_pattern regex: {e}",
                    error=f"Invalid regex: {e}",
                )

        # Filter and count matching commands
        matching_commands: list[str] = []
        for cmd in all_commands:
            # Filter by tool name
            if criterion.tool_name is not None and cmd.tool_name != criterion.tool_name:
                continue

            # Filter by success status
            if criterion.require_success and cmd.result_status != "success":
                continue

            # Extract text for pattern matching (Bash: command param; others: JSON-serialized params)
            if cmd.tool_name == "Bash" and cmd.parameters.get("command"):
                cmd_text = cmd.parameters["command"]
            else:
                cmd_text = json.dumps(cmd.parameters)

            # Truncate to mitigate ReDoS on large command strings
            if len(cmd_text) > _MAX_PATTERN_SEARCH_LEN:
                cmd_text = cmd_text[:_MAX_PATTERN_SEARCH_LEN]

            # Filter by command pattern
            if pattern is not None and not pattern.search(cmd_text):
                continue

            # Apply exclusion pattern (skip commands matching the exclusion)
            if exclude_re is not None and exclude_re.search(cmd_text):
                continue

            # Build a display label for the matched command
            if cmd.tool_name == "Bash" and cmd.parameters.get("command"):
                label = cmd.parameters["command"]
            else:
                label = f"{cmd.tool_name}({json.dumps(cmd.parameters)[:80]})"
            matching_commands.append(label)

        match_count = len(matching_commands)
        score = min(1.0, match_count / criterion.min_count)

        # Build details
        filters = []
        if criterion.tool_name is not None:
            filters.append(f"tool_name={criterion.tool_name}")
        if criterion.command_pattern is not None:
            filters.append(f"pattern=/{criterion.command_pattern}/")
        if criterion.require_success:
            filters.append("require_success=True")
        if criterion.exclude_pattern is not None:
            filters.append(f"exclude=/{criterion.exclude_pattern}/")
        filter_text = ", ".join(filters) if filters else "none"

        details = f"Matched {match_count}/{criterion.min_count} required commands (filters: {filter_text})"
        if matching_commands:
            # Show up to 3 example matches
            examples = matching_commands[:3]
            truncated = [ex[:120] for ex in examples]
            details += f"\nExamples: {truncated}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
