"""CLI-called criterion checker — structured matching over an invocation log."""

import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.invocation_log import parse_log
from coder_eval.models import CliCalledCriterion, CriterionResult, FlagMatch


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _split_flags(
    argv: list[str],
    ignore: frozenset[str],
    value_flags: frozenset[str],
    known_names: frozenset[str] = frozenset(),
) -> tuple[list[str], dict[str, list[str]]]:
    """Split ``argv`` into non-flag arguments and a flag map.

    Only flags in ``value_flags`` consume a following token; everything else is a
    switch. Guessing instead (``--yes proj-1`` binding ``yes=proj-1``) let a
    ``max_count: 0`` guard pass on the delete it forbade, so ambiguity resolves
    toward keeping the token positional.

    ``--flag=value`` binds directly, being unambiguous. Repeated flags accumulate.
    ``ignore`` names are dropped with their values. ``--`` ends flag parsing and is
    itself dropped; a lone ``-`` is positional.

    ``known_names`` are the flag names the criterion mentions at all (including
    presence predicates and aliases). A declared name is always taken whole, so a
    genuine multi-char short flag still matches; undeclared ones are split.
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
        known = name in value_flags or name in known_names

        # A bare negative number is a value, not a flag. Reading `-1` as a flag
        # named `1` drops it from the positionals -- the same silent-disappearance
        # that let `--yes proj-1` slip a delete past a guard.
        if not known and _is_number(name):
            positional.append(token)
            continue

        # Clustered short flags: `-rf` is `-r -f`. Declared names win, so a real
        # multi-char short flag still matches, and `-fvalue` binds when `f` takes
        # a value; otherwise each character is its own switch, which is what stops
        # `-yf` escaping an `aliases: [y]` predicate.
        if not known and not token.startswith("--") and len(name) > 1:
            head, rest = name[0], name[1:]
            if head in value_flags:
                record(head, rest)
            else:
                for char in name:
                    record(char, "")
            continue

        if name in value_flags and index < len(argv):
            record(name, argv[index])
            index += 1
        else:
            # Switch: empty value, and the next token is left for the positionals.
            record(name, "")

    return positional, flags


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _flag_matches(predicate: FlagMatch, values: list[str] | None) -> bool:
    """Whether a recorded flag satisfies one :class:`FlagMatch` predicate.

    ``values`` is None when the flag was not passed at all. Every non-``absent``
    predicate is satisfied by ANY of a repeated flag's values.
    """
    if predicate.absent:
        return values is None
    if predicate.present:
        return values is not None
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


def _record_matches(criterion: CliCalledCriterion, argv: list[str], record: dict[str, Any]) -> bool:
    """Whether one log record satisfies every configured facet of the criterion."""
    if criterion.tool is not None and record.get("tool") != criterion.tool:
        return False

    # Declarations only. Folding `ignore_flags` in here made ignored SWITCHES
    # value-bearing, which swallowed the next positional and reopened the guard
    # false-PASS; an ignored flag that takes a value declares it in value_flags.
    positional, flags = _split_flags(
        argv,
        frozenset(criterion.ignore_flags),
        frozenset(n for name, p in (criterion.flags or {}).items() if p.needs_value for n in (name, *p.aliases))
        | frozenset(criterion.value_flags),
        frozenset(n for name, p in (criterion.flags or {}).items() for n in (name, *p.aliases))
        | frozenset(criterion.ignore_flags),
    )

    offset = 0
    spellings = criterion.verb_spellings
    if spellings:
        # ORDERED prefix compared token by token — not a subset, and not a string
        # startswith: `labellings confirm` must never be satisfied by
        # `labellings unconfirm`, nor `projects list` by `projects lists`.
        matched = next((tokens for tokens in spellings if positional[: len(tokens)] == tokens), None)
        if matched is None:
            return False
        # Offset comes from the candidate that matched, since spellings may differ in
        # length. Validation rejects one spelling being a prefix of another, so at
        # most one can match and this cannot depend on list order.
        offset = len(matched)

    if criterion.positional is not None:
        expected = criterion.positional
        if positional[offset : offset + len(expected)] != expected:
            return False

    if criterion.flags:
        for name, predicate in criterion.flags.items():
            # [] means absent under every spelling, which _flag_matches
            # distinguishes from a switch's "present with empty value" ([""]).
            collected = [v for n in (name, *predicate.aliases) for v in flags.get(n, [])]
            if not _flag_matches(predicate, collected or None):
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
        # Same source as the matcher, so the detail can never describe a different
        # constraint than the one applied. Whitespace is normalized on the way through
        # (`'a  b'` renders as `'a b'`), which matches how the tokens were compared.
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
