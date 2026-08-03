"""Command executed criterion checker."""

import json
import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, LiveVerdict, register_criterion
from coder_eval.models import CommandExecutedCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.telemetry import CommandTelemetry
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

    # Observable mid-run: command matches accumulate monotonically in the live
    # stream, so a min_count pass (no upper bound) and a max_count exceedance
    # (incl. the must-NOT-run 0/0 form) are both decidable before end-of-run.
    # Per-instance decidability narrowing now lives on the model
    # (``CommandExecutedCriterion.live_decidable_polarities`` in
    # models/criteria.py) — "is this criterion type live-observable" is its
    # ``LiveSuccessCriterion`` subclassing, not a checker-side ClassVar.

    @staticmethod
    def _matching_commands(
        criterion: CommandExecutedCriterion,
        all_commands: list["CommandTelemetry"],
        pattern: re.Pattern[str] | None,
        exclude_re: re.Pattern[str] | None,
    ) -> list[str]:
        """Filter + label the commands matching this criterion.

        Shared by ``_check_impl`` and ``live_verdict`` so a command can never
        count for one and not the other (the live trigger and the authoritative
        score always agree on WHICH commands match). Callers compile the
        patterns and own error handling.
        """
        matching: list[str] = []
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
            matching.append(label)
        return matching

    def live_verdict(
        self,
        criterion: CommandExecutedCriterion,
        turn_records: list["TurnRecord"],
    ) -> LiveVerdict:
        """Decide from the partial trajectory (early-stop trigger).

        - ``fail`` the moment ``match_count`` exceeds ``max_count`` — monotone
          (once over, always over). Covers the ``min_count: 0, max_count: 0``
          "must NOT run" form: the first forbidden match is a definitive fail.
        - ``pass`` the moment ``match_count`` reaches ``min_count`` when there is
          no upper bound (``max_count is None`` and ``min_count > 0``). With an
          upper bound the pass is only final at end-of-run, so it stays
          ``undecided`` here.
        - otherwise ``undecided``.

        A malformed regex yields ``undecided`` (the final ``_check_impl`` surfaces
        the config error as a scored failure).
        """
        all_commands = [cmd for turn in turn_records for cmd in turn.commands]
        try:
            pattern = (
                re.compile(criterion.command_pattern, re.DOTALL) if criterion.command_pattern is not None else None
            )
            exclude_re = (
                re.compile(criterion.exclude_pattern, re.DOTALL) if criterion.exclude_pattern is not None else None
            )
        except re.error:
            return "undecided"

        match_count = len(self._matching_commands(criterion, all_commands, pattern, exclude_re))
        if criterion.max_count is not None and match_count > criterion.max_count:
            return "fail"
        if criterion.max_count is None and criterion.min_count > 0 and match_count >= criterion.min_count:
            return "pass"
        return "undecided"

    def _check_impl(
        self,
        criterion: CommandExecutedCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
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

        all_commands = [cmd for turn in turn_records for cmd in turn.commands]

        # Note: do NOT short-circuit when ``all_commands`` is empty. A
        # negative-assertion criterion (``min_count: 0`` + ``max_count: 0``)
        # SHOULD pass here — zero commands trivially satisfies "must not
        # call X". Falling through into the matching loop sets
        # ``match_count = 0`` and the scoring branch handles both shapes.

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

        # Filter and count matching commands (shared with live_verdict so the
        # live trigger and the authoritative score never disagree on matches).
        matching_commands = self._matching_commands(criterion, all_commands, pattern, exclude_re)

        match_count = len(matching_commands)

        # Score model:
        #   max_count is None  →  fractional towards min_count (legacy behavior).
        #                         When min_count == 0, the criterion is trivially
        #                         satisfied (no minimum to hit) and scores 1.0.
        #   max_count is set   →  binary in-range. Pass iff
        #                         min_count <= match_count <= max_count.
        # The "negative assertion" pattern (min_count: 0, max_count: 0) drops
        # naturally out of the binary branch — it now expresses "must NOT match"
        # exactly. The model validator already rejects max_count < min_count.
        if criterion.max_count is None:
            score = 1.0 if criterion.min_count == 0 else min(1.0, match_count / criterion.min_count)
        else:
            score = 1.0 if criterion.min_count <= match_count <= criterion.max_count else 0.0

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

        if criterion.max_count is None:
            range_text = f"{match_count}/{criterion.min_count} required"
        else:
            range_text = f"{match_count} matches (allowed range {criterion.min_count}..{criterion.max_count})"
        details = f"Matched {range_text} commands (filters: {filter_text})"
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
