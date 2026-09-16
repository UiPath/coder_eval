"""Command executed criterion checker."""

import json
import logging
import re
import shlex
from functools import lru_cache
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, LiveVerdict, register_criterion
from coder_eval.models import CommandExecutedCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.telemetry import CommandTelemetry
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# HAZARD: bounds ReDoS on a large command string. Normalization runs over this
# same truncated window, so shlex needs no separate size guard.
_MAX_PATTERN_SEARCH_LEN = 2000


def _is_shell_program(arg0: str) -> bool:
    """True if argv[0]'s basename looks like a POSIX shell.

    A predicate rather than an enumerated allowlist: the set of shells is open
    (``bash``/``sh`` on Linux, ``zsh`` on macOS — Codex shells through the host's
    login shell, see codex_agent.py — plus ``dash``/``ksh``/…), and every common
    shell basename ends in ``sh``. Matching is additive (the raw text stays a
    haystack), so favouring recall over a hand-maintained list is safe.
    """
    return arg0.rsplit("/", 1)[-1].endswith("sh")


def _is_command_flag(tok: str) -> bool:
    """True for a short-option cluster carrying a ``-c`` command string.

    Covers ``-c``, ``-lc``, ``-ic``, ``-lic`` (login/interactive + command) in
    any order — a single ``-``-prefixed token whose option letters are all
    alphabetic and include ``c``. ``--long`` options and ``-o=val`` forms are
    rejected, so this is the first flag that actually introduces the command
    string. The combined and split (``bash -l -c``) forms both work: a non-``c``
    short flag like ``-l`` simply isn't the command flag and the scan continues.
    """
    return len(tok) >= 2 and tok[0] == "-" and tok[1] != "-" and tok[1:].isalpha() and "c" in tok[1:]


@lru_cache(maxsize=1024)
def _normalize_shell(cmd_text: str) -> str | None:
    """Quote-resolved, wrapper-stripped form of a shell command, or None.

    Unwraps a ``bash``/``sh``/``zsh -c`` wrapper and resolves shell quoting with
    ``shlex``, so a ``command_pattern`` can match argv semantics regardless of how
    the agent happened to quote an argument. Shell operators survive as their own
    tokens; embedded newlines collapse to single spaces. Returns ``None`` when the
    text cannot be parsed (an odd quote count -- NOT heredocs, which tokenize
    fine); the caller then keeps only the raw text as a haystack.

    NOT purely additive: the same haystacks feed ``exclude_pattern`` and the
    ``max_count`` gate, so a command that counted on the raw text alone can stop
    counting.

    Memoized -- a pure function of ``cmd_text``.

    Rationale: .claude/notes/contracts.md § Normalizing a shell command before matching it
    """
    try:
        tokens = shlex.split(cmd_text, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    # Unwrap `bash -lc "<script>"` / `sh -c "<script>"`: the real command is
    # everything after the -c/-lc flag.
    if _is_shell_program(tokens[0]):
        for i in range(1, len(tokens) - 1):
            tok = tokens[i]
            if _is_command_flag(tok):
                rest = tokens[i + 1 :]
                if len(rest) == 1:
                    # Quoted whole-script form: one token that may hold inner
                    # quotes -- re-split to resolve them.
                    try:
                        tokens = shlex.split(rest[0], posix=True)
                    except ValueError:
                        return None
                else:
                    # Argv-joined form: Codex rollout recovery joins argv without
                    # re-quoting, so it already arrives split.
                    tokens = rest
                break
            if not tok.startswith("-"):
                break  # first positional before any -c: not a command wrapper
    return " ".join(tokens)


def _match_haystacks(cmd_text: str, *, is_shell: bool) -> list[str]:
    """Strings a pattern may match against for one command.

    Always the raw ``cmd_text`` truncated to the ReDoS bound; when ``is_shell``,
    additionally the quote-resolved, wrapper-stripped form of that **same
    truncated window** (see :func:`_normalize_shell`). Normalizing the already-
    truncated slice keeps both haystacks describing the same window, so quote-
    stripping can never slide content from past the cap into the match, and
    caps ``shlex`` input at ``_MAX_PATTERN_SEARCH_LEN`` for free. Matching is
    "either" — a pattern hits the command if it matches ANY haystack.

    ``is_shell`` is decided once by the caller (a Bash tool whose ``command`` is
    a non-empty ``str``) and passed in, rather than re-derived here from
    ``tool_name`` alone: a Bash record with a missing/empty ``command`` serializes
    its params to JSON, where shell tokenization is meaningless, and must NOT be
    normalized (else stripped JSON quotes could newly satisfy an exclusion).
    """
    window = cmd_text[:_MAX_PATTERN_SEARCH_LEN]
    haystacks = [window]
    if is_shell:
        normalized = _normalize_shell(window)
        if normalized is not None and normalized != window:
            haystacks.append(normalized)
    return haystacks


@register_criterion
class CommandExecutedChecker(BaseCriterion[CommandExecutedCriterion]):
    """Checker for CommandExecutedCriterion.

    Inspects agent CommandTelemetry records to verify specific
    tools/commands were used during evaluation.
    """

    criterion_type = "command_executed"

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

            # A ``command`` value is not guaranteed to be a ``str`` -- Codex rollout
            # recovery can carry it as an argv LIST. Narrow with ``isinstance`` so a
            # non-str never reaches ``shlex.split`` and zeroes the whole criterion.
            raw_command = cmd.parameters.get("command")
            if cmd.tool_name == "Bash" and isinstance(raw_command, str) and raw_command:
                cmd_text = raw_command
                is_shell = True
            else:
                cmd_text = json.dumps(cmd.parameters)
                is_shell = False

            # Raw command AND its quote-resolved form. ``is_shell`` is decided once
            # above; the helper must not re-derive it from tool_name alone.
            # Rationale: .claude/notes/contracts.md § Normalizing a shell command before matching it
            haystacks = _match_haystacks(cmd_text, is_shell=is_shell)

            # Filter by command pattern
            if pattern is not None and not any(pattern.search(h) for h in haystacks):
                continue

            # Apply exclusion pattern (skip commands matching the exclusion).
            # Same both-haystacks logic so exclusion can't be dodged by quoting.
            if exclude_re is not None and any(exclude_re.search(h) for h in haystacks):
                continue

            # Build a display label for the matched command (same narrowing as above)
            if cmd.tool_name == "Bash" and isinstance(raw_command, str) and raw_command:
                label = raw_command
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
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Check if the agent executed commands matching the criterion filters.

        Scoring is fractional: min(1.0, matching_count / min_count).

        Args:
            criterion: Command executed criterion with filters
            sandbox: Sandbox instance (not used for this criterion)
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

        # Do NOT short-circuit on an empty list: a negative assertion
        # (``min_count: 0`` + ``max_count: 0``) SHOULD pass, and falling through
        # sets ``match_count = 0`` so the scoring branch handles both shapes.

        # re.DOTALL so `.` matches newlines: agents write multi-line bash with
        # backslash continuations, and `foo.*--body` must span them.
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

        # Score model: no max_count -> fractional towards min_count (min_count 0 is
        # trivially satisfied, scoring 1.0); max_count set -> binary in-range. The
        # negative assertion falls out of the binary branch.
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
