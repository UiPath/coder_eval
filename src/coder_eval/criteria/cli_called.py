"""CLI-called criterion checker — structured matching over an invocation log."""

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CliCalledCriterion, CriterionResult, FlagMatch


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _split_flags(
    argv: list[str],
    ignore: frozenset[str],
    value_flags: frozenset[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Split ``argv`` into non-flag arguments and a flag map.

    Value binding is DECLARED, not guessed. ``value_flags`` names the flags that
    consume a following token; every other flag is a switch whose following token
    stays positional. The criterion supplies its own ``flags:`` keys plus
    ``value_flags:`` as that set, so the author's assertion doubles as the grammar.

    This replaced a heuristic ("the next token is the value unless it starts with
    ``-``") that silently swallowed a positional after a boolean switch. For
    ``uip fields delete --yes proj-1`` it bound ``yes=proj-1`` and dropped
    ``proj-1`` from the positionals, so a ``max_count: 0`` guard on
    ``positional: [proj-1]`` reported a PASS while the log proved the delete had
    happened. ``--yes``/``--force``/``-y`` before the target is how destructive
    CLIs are invoked, which is exactly the shape a negative guard exists to catch,
    so the default resolves ambiguity toward keeping the token positional.

    Other normalizations, each a case a flat-string regex gets wrong:

    - ``--flag=value`` binds directly. The equals form is unambiguous, so it is
      never re-run through any value/switch decision: ``--offset=-1`` keeps ``-1``
      instead of dropping it and inventing a flag named ``1``.
    - A declared value flag consumes its next token even when that token starts
      with ``-``, so ``--limit -1`` binds ``-1``.
    - Repeated flags accumulate, so ``--field a --field b`` keeps both values.
    - Names in ``ignore`` are dropped along with their values.

    A bare ``--`` terminates flag parsing (POSIX convention): everything after it
    is positional, and the separator itself is dropped so it never has to be
    written into a ``positional:`` expectation. A lone ``-`` is positional too
    (the stdin convention), not a flag.
    """
    positional: list[str] = []
    flags: dict[str, list[str]] = {}

    def record(name: str, value: str) -> None:
        if name not in ignore:
            flags.setdefault(name, []).append(value)

    index = 0
    end_of_flags = False
    while index < len(argv):
        token = argv[index]
        index += 1

        if end_of_flags or not token.startswith("-") or token == "-":
            positional.append(token)
            continue
        if token == "--":
            end_of_flags = True
            continue

        # Equals form: unambiguous, bind it and move on.
        if "=" in token:
            name, _, value = token.partition("=")
            record(name.lstrip("-"), value)
            continue

        name = token.lstrip("-")
        if name in value_flags and index < len(argv):
            record(name, argv[index])
            index += 1
        else:
            # Switch: empty value, and the next token is left for the positionals.
            record(name, "")

    return positional, flags


def _flag_matches(predicate: FlagMatch, values: list[str] | None) -> bool:
    """Whether a recorded flag satisfies one :class:`FlagMatch` predicate.

    ``values`` is None when the flag was not passed at all. Every non-``absent``
    predicate is satisfied by ANY of a repeated flag's values.
    """
    if predicate.absent:
        return values is None
    if values is None:
        return False
    if predicate.equals is not None:
        return any(value == predicate.equals for value in values)
    if predicate.contains is not None:
        return any(predicate.contains in value for value in values)
    if predicate.any_of is not None:
        allowed = set(predicate.any_of)
        return any(value in allowed for value in values)
    if predicate.matches_regex is not None:
        regex = re.compile(predicate.matches_regex, predicate.flags)
        return any(regex.search(value) is not None for value in values)
    # Unreachable: FlagMatch guarantees exactly one predicate. Raise rather than
    # return False so a predicate added without a matcher arm here fails loudly.
    raise AssertionError(f"FlagMatch has no matcher arm: {predicate!r}")


def _usable_argv(record: dict[str, Any]) -> list[str] | None:
    """The record's ``argv`` when it is a list of strings, else None.

    None means the record cannot be evaluated at all — a different thing from
    "evaluated and did not match", which is why the caller reports it rather than
    quietly treating it as a non-match.
    """
    argv = record.get("argv")
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        return argv
    return None


def _record_matches(criterion: CliCalledCriterion, argv: list[str], record: dict[str, Any]) -> bool:
    """Whether one log record satisfies every configured facet of the criterion."""
    if criterion.tool is not None and record.get("tool") != criterion.tool:
        return False

    # The criterion's own flag predicates declare which flags carry a value;
    # `value_flags` covers the rest (a flag whose value must not be mistaken for a
    # positional even though nothing asserts on it).
    ignore = frozenset(criterion.ignore_flags)
    positional, flags = _split_flags(
        argv,
        ignore,
        # Ignored flags are value-bearing too: dropping `--output` while leaving
        # `json` in the positionals would defeat the point of ignoring it.
        frozenset(criterion.flags or {}) | frozenset(criterion.value_flags) | ignore,
    )

    offset = 0
    if criterion.verb is not None:
        verb_tokens = criterion.verb.split()
        # ORDERED prefix, not a token subset: `labellings confirm` must never be
        # satisfied by `labellings unconfirm`, and a project name that happens to
        # equal a subcommand must not stand in for the subcommand.
        if positional[: len(verb_tokens)] != verb_tokens:
            return False
        offset = len(verb_tokens)

    if criterion.positional is not None:
        expected = criterion.positional
        if positional[offset : offset + len(expected)] != expected:
            return False

    if criterion.flags:
        for name, predicate in criterion.flags.items():
            if not _flag_matches(predicate, flags.get(name)):
                return False

    return True


@register_criterion
class CliCalledChecker(BaseCriterion[CliCalledCriterion]):
    """Checker for CliCalledCriterion."""

    criterion_type = "cli_called"

    def _check_impl(
        self,
        criterion: CliCalledCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Count invocations in the structured log that match the criterion.

        Args:
            criterion: CLI-called criterion
            sandbox: Sandbox instance for file access
            reference_code: Not used for this criterion

        Returns:
            Result with binary score (1.0 when the match count is within
            [min_count, max_count], 0.0 otherwise)
        """
        # Compile every flag regex up front so a bad pattern reports itself as
        # such, instead of surfacing as a generic caught exception once the first
        # record happens to reach that predicate.
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
            # A missing log is a harness fault, not agent behaviour: the mock never
            # ran or wrote elsewhere. Failing (rather than treating it as "zero
            # matching calls") is what stops a negative guard — max_count: 0 —
            # from passing vacuously against a log that does not exist.
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"Invocation log '{criterion.log}' does not exist",
            )

        content = sandbox.get_file_content(criterion.log)

        usable: list[tuple[list[str], dict[str, Any]]] = []
        unusable = 0
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                unusable += 1
                continue
            if not isinstance(parsed, dict):
                unusable += 1
                continue
            argv = _usable_argv(parsed)
            if argv is None:
                unusable += 1
                continue
            usable.append((argv, parsed))

        if unusable:
            # Same footing as a missing log, and for the same reason: a record we
            # cannot read might BE the invocation a max_count: 0 guard forbids, so
            # scoring it as "did not match" would let the guard pass on the very
            # call it exists to catch. Skipping these silently (the previous
            # behaviour) contradicted the fail-loud missing-log path above and the
            # sibling precedent in json_check.
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
        if criterion.verb is not None:
            facets.append(f"verb={criterion.verb!r}")
        if criterion.positional is not None:
            facets.append(f"positional={criterion.positional!r}")
        if criterion.flags:
            facets.append(f"flags={sorted(criterion.flags)}")
        wanted = ", ".join(facets)

        if score == 1.0:
            details = f"{count} invocation(s) matched ({wanted}); satisfies {bound}"
        elif not within_lower:
            details = (
                f"{count} invocation(s) matched ({wanted}); needs {bound}. "
                f"{len(records)} invocation(s) recorded in '{criterion.log}'"
            )
        else:
            details = f"{count} invocation(s) matched ({wanted}) but {bound} forbids it"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
