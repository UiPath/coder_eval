"""CLI-called criterion checker — structured matching over an invocation log."""

import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

from coder_eval.argv_match import argv_matches
from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.invocation_log import parse_log
from coder_eval.models import CliCalledCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _record_matches(criterion: CliCalledCriterion, argv: list[str], record: dict[str, Any]) -> bool:
    """Whether one log record satisfies every configured facet of the criterion.

    ``tool`` is checked here rather than in :func:`argv_matches` because it is a
    property of the RECORD, not of the arguments -- the shim that serves a
    response knows which tool it is before it looks at argv.
    """
    if criterion.tool is not None and record.get("tool") != criterion.tool:
        return False
    return argv_matches(criterion.match_spec, argv)


@register_criterion
class CliCalledChecker(BaseCriterion[CliCalledCriterion]):
    """Checker for CliCalledCriterion."""

    criterion_type = "cli_called"

    def _check_impl(
        self,
        criterion: CliCalledCriterion,
        sandbox: "Sandbox",
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Count invocations in the structured log that match the criterion.

        Args:
            criterion: CLI-called criterion
            sandbox: Sandbox instance for file access

        Returns:
            Result with binary score (1.0 when the match count is within
            [min_count, max_count], 0.0 otherwise)
        """
        # Up front so a bad pattern names its flag, rather than surfacing as a
        # generic caught exception when some record first reaches that predicate.
        for name, predicate in (criterion.flags or {}).items():
            if predicate.matches_regex is None:
                continue
            try:
                re.compile(predicate.matches_regex, predicate.flags)
            except (re.error, ValueError) as exc:
                return CriterionResult(
                    criterion_type=criterion.type,
                    description=criterion.description,
                    score=0.0,
                    error=f"Invalid matches_regex for flag '{name}': {exc}",
                )

        if not sandbox.file_exists(criterion.log):
            # Harness fault, not agent behaviour. Failing stops a max_count: 0
            # guard passing vacuously against a log that never existed.
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"Invocation log '{criterion.log}' does not exist",
            )

        # The recorder leaves this beside the log when a write failed, so a record
        # it could not append does not read as "the agent never ran the command".
        sentinel = f"{criterion.log}.error"
        if sandbox.file_exists(sentinel):
            detail = sandbox.get_file_content(sentinel).strip().splitlines()
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=(
                    f"Recorder could not write to '{criterion.log}' ({len(detail)} dropped record(s)); "
                    f"the log is incomplete so the verdict cannot be trusted. First: {detail[0] if detail else '?'}"
                ),
            )

        content = sandbox.get_file_content(criterion.log)

        usable, unusable = parse_log(content)

        if unusable:
            # A record we cannot read might BE the call a max_count: 0 guard
            # forbids, so scoring it "did not match" would let the guard pass.
            logger.warning(
                f"cli_called: {unusable} unusable record(s) in '{criterion.log}'"
                + " (unparseable line, non-object line, or argv that is not a list of strings)"
            )
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=(
                    f"Invocation log '{criterion.log}' has {unusable} unusable record(s): a line that is "
                    "not JSON, not an object, or whose 'argv' is not a list of strings. The verdict "
                    "cannot be trusted, so the criterion fails rather than scoring an incomplete log."
                ),
            )

        matches = [record for argv, record in usable if _record_matches(criterion, argv, record)]
        count = len(matches)
        records = usable

        within_lower = count >= criterion.min_count
        within_upper = criterion.max_count is None or count <= criterion.max_count
        score = 1.0 if within_lower and within_upper else 0.0

        bound = f"min_count={criterion.min_count}"
        if criterion.max_count is not None:
            bound += f", max_count={criterion.max_count}"

        facets = []
        if criterion.tool is not None:
            facets.append(f"tool={criterion.tool!r}")
        # Reading `criterion.verb` here would print no verb at all for a `verb_any_of`
        # criterion, hiding the constraint that caused the failure.
        if spellings := criterion.verb_spellings:
            facets.append(f"verb={' | '.join(' '.join(t) for t in spellings)!r}")
        if criterion.positional is not None:
            facets.append(f"positional={criterion.positional!r}")
        if criterion.flags:
            facets.append(f"flags={sorted(criterion.flags)}")
        wanted = ", ".join(facets)

        if score == 1.0:
            details = f"{count} invocation(s) matched ({wanted}); satisfies {bound}"
        elif not within_lower:
            # A bare count sends the reader to the sandbox; this criterion exists
            # to answer "what did it actually run".
            sample = "; ".join(shlex.join(argv)[:120] for argv, _ in usable[:3])
            more = f" (+{len(usable) - 3} more)" if len(usable) > 3 else ""
            recorded = f" Recorded: {sample}{more}" if sample else ""
            details = (
                f"{count} invocation(s) matched ({wanted}); needs {bound}. "
                f"{len(records)} invocation(s) recorded in '{criterion.log}'.{recorded}"
            )
        else:
            details = f"{count} invocation(s) matched ({wanted}) but {bound} forbids it"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
