"""CLI-called criterion checker — structured matching over an invocation log."""

import logging
import shlex
from typing import TYPE_CHECKING

from coder_eval.argv_match import argv_matches
from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.invocation_log import parse_log
from coder_eval.models import CliCalledCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _record_matches(criterion: CliCalledCriterion, argv: list[str], record: dict[str, object]) -> bool:
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
        # No pre-flight re.compile: `FlagMatch` compiles at validation, which is
        # where it has to happen -- the response-rule surface cannot report.
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

        # Scoped to the records this criterion is about: one log serves every
        # shadowed tool.
        mine = [record for _, record in usable if criterion.tool is None or record.get("tool") == criterion.tool]

        # The shim could not IMPORT its matcher, so no rule was ever tried and the
        # agent saw the entry defaults throughout. Scored 0.0, never raised -- the
        # RECORDS stay trustworthy (the shim keeps logging), which is what stops a
        # `max_count: 0` guard passing on a call that went unrecorded.
        broken = [record for record in mine if record.get("sidecar_error") is not None]
        if broken:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=(
                    f"Recorder could not import its matcher module on {len(broken)} invocation(s), so no "
                    f"response rule was tried and the agent saw the entry defaults throughout. "
                    f"First: {broken[0].get('sidecar_error')!r}"
                ),
            )

        # Booked when the shim's own rule evaluation RAISED: the responses the agent
        # saw were not the ones the task described, so no verdict over this log
        # means anything.
        #
        # HAZARD: all five of this checker's refuse-to-score paths are uniform at a
        # gating 0.0 and NONE may raise -- every one is agent-reachable, and an
        # escalation to ERROR is a better outcome for a failing agent than FAILED.
        # Rationale: .claude/notes/contracts.md § The five refuse-to-score paths are uniform at a gating 0.0
        faults = [record for record in mine if record.get("rule_error") is not None]
        if faults:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=(
                    f"Recorder could not evaluate its response rules on {len(faults)} invocation(s), so the "
                    f"agent saw fallback output the task did not describe. The log cannot be trusted. "
                    f"First: {faults[0].get('rule_error')!r}"
                ),
            )

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
