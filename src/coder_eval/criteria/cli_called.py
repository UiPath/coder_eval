"""CLI-called criterion checker — structured matching over an invocation log."""

import json
import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CliCalledCriterion, CriterionResult, FlagMatch


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _split_flags(argv: list[str], ignore: frozenset[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split ``argv`` into non-flag arguments and a flag map.

    Normalizations, each one a case that a flat-string regex gets wrong:

    - ``--flag=value`` is split into ``--flag value``, so the equals-form and the
      space-form compare equal.
    - A flag's value is the following token unless that token itself starts with
      ``-``, in which case the flag is treated as a boolean switch. Without a CLI
      grammar this is the only available heuristic; the ambiguity is confined to
      here rather than duplicated into every task's pattern.
    - Repeated flags accumulate, so ``--field a --field b`` keeps both values.
    - Names in ``ignore`` are dropped with their values.

    A bare ``--`` terminates flag parsing (POSIX convention): everything after it
    is positional, and the separator itself is dropped so it never has to be
    written into a ``positional:`` expectation. A lone ``-`` is positional too
    (the stdin convention), not a flag.
    """
    positional: list[str] = []
    flags: dict[str, list[str]] = {}

    tokens: list[str] = []
    end_of_flags = False
    for raw in argv:
        if end_of_flags:
            tokens.append(raw)
            continue
        if raw == "--":
            end_of_flags = True
            tokens.append(raw)
            continue
        if raw.startswith("-") and "=" in raw:
            name, _, value = raw.partition("=")
            tokens.append(name)
            tokens.append(value)
        else:
            tokens.append(raw)

    index = 0
    end_of_flags = False
    while index < len(tokens):
        token = tokens[index]
        if token == "--" and not end_of_flags:
            end_of_flags = True
            index += 1
            continue
        if not end_of_flags and token.startswith("-") and token != "-":
            name = token.lstrip("-")
            flag_value: str | None = None
            if index + 1 < len(tokens):
                candidate = tokens[index + 1]
                if not candidate.startswith("-") or candidate == "-":
                    flag_value = candidate
                    index += 1
            if name not in ignore:
                flags.setdefault(name, []).append(flag_value if flag_value is not None else "")
            index += 1
            continue
        positional.append(token)
        index += 1

    return positional, flags


@lru_cache(maxsize=256)
def _compiled(pattern: str, flags: int) -> re.Pattern[str]:
    """Compile once per (pattern, flags) rather than once per record per flag.

    A log can hold hundreds of invocations; recompiling the same pattern for each
    is pure waste. Cached at module scope because patterns come from task YAML and
    are few and long-lived.
    """
    return re.compile(pattern, flags)


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
        regex = _compiled(predicate.matches_regex, predicate.flags)
        return any(regex.search(value) is not None for value in values)
    return False


def _record_matches(criterion: CliCalledCriterion, record: dict[str, Any]) -> bool:
    """Whether one log record satisfies every configured facet of the criterion."""
    if criterion.tool is not None and record.get("tool") != criterion.tool:
        return False

    argv = record.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return False

    positional, flags = _split_flags(argv, frozenset(criterion.ignore_flags))

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
                _compiled(predicate.matches_regex, predicate.flags)
            except re.error as exc:
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

        records: list[dict[str, Any]] = []
        malformed = 0
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                malformed += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                malformed += 1

        matches = [record for record in records if _record_matches(criterion, record)]
        count = len(matches)

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

        if malformed:
            details += f". Skipped {malformed} unparseable log line(s)"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
