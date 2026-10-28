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

# HAZARD: bounds ReDoS on a large command string. Every search window is at most
# this long and normalization runs per window, so shlex needs no separate size guard.
_MAX_PATTERN_SEARCH_LEN = 2000
_WINDOW_OVERLAP = 256
_LOGICAL_LINE_END = re.compile(r"(?<!\\)\n")


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


def _search_windows(cmd_text: str) -> list[str]:
    """Slices of ``cmd_text`` a pattern is searched in, each at most ``_MAX_PATTERN_SEARCH_LEN``.

    The first window is the leading ``_MAX_PATTERN_SEARCH_LEN`` characters, so every
    match the single leading window found is still found. The rest of the command is
    packed into windows that start and end on logical-line boundaries (a
    backslash-continued line stays whole), so a command on a line after a long heredoc
    is still seen. A single logical line longer than the bound is cut into bound-sized
    pieces overlapping by ``_WINDOW_OVERLAP``, so a match up to that long is never
    split. Work stays linear in the command length and each regex search stays within
    the ReDoS bound.
    """
    cap = _MAX_PATTERN_SEARCH_LEN
    if len(cmd_text) <= cap:
        return [cmd_text]
    bounds: list[tuple[int, int]] = []
    start = end = 0
    for line_end in [m.end() for m in _LOGICAL_LINE_END.finditer(cmd_text)] + [len(cmd_text)]:
        if line_end - start > cap:
            if end > start:
                bounds.append((start, end))
                start = end
            while line_end - start > cap:
                bounds.append((start, start + cap))
                start += cap - _WINDOW_OVERLAP
        end = line_end
    if end > start:
        bounds.append((start, end))
    return [cmd_text[:cap]] + [cmd_text[s:e] for s, e in bounds if s > 0]


def _match_haystacks(cmd_text: str, *, is_shell: bool) -> list[str]:
    """Strings a pattern may match against for one command.

    When ``is_shell`` (``cmd_text`` is a shell command string), every search window of
    ``cmd_text`` (see :func:`_search_windows`) plus the quote-resolved,
    wrapper-stripped form of each window (see :func:`_normalize_shell`). Otherwise
    ``cmd_text`` is JSON and only its leading ``_MAX_PATTERN_SEARCH_LEN`` characters
    are searched: a later window of a Write/Edit body is file content, not something
    the agent ran. JSON is not normalized, because stripped JSON quotes could newly
    satisfy an exclusion. Normalizing per window keeps every haystack within the ReDoS
    bound and caps ``shlex`` input for free. Matching is "either" -- a pattern hits
    the command if it matches ANY haystack.

    The caller decides ``is_shell`` in :func:`_search_text`.
    """
    if not is_shell:
        return [cmd_text[:_MAX_PATTERN_SEARCH_LEN]]
    haystacks: list[str] = []
    for window in _search_windows(cmd_text):
        haystacks.append(window)
        normalized = _normalize_shell(window)
        if normalized is not None and normalized != window:
            haystacks.append(normalized)
    return haystacks


def _is_shell_call(cmd: "CommandTelemetry") -> bool:
    """True for a ``Bash`` call or any call with a ``command`` param (a shell tool under another name)."""
    return cmd.tool_name == "Bash" or "command" in cmd.parameters


def _search_text(cmd: "CommandTelemetry", *, tool_named: bool) -> tuple[str, bool] | None:
    """The text a pattern searches in one call, and whether it is a shell command string.

    The shape of the call decides, not the criterion. A shell call (:func:`_is_shell_call`)
    is searched on its command:

    - a non-empty ``str`` command is searched with shell normalization and windows;
    - another non-empty command (an argv list) is searched as its JSON;
    - a missing or empty command gives no text.

    Another call is searched on its JSON params only when the criterion names its tool
    (``tool_named``); otherwise it gives no text, because a Write body or a TodoWrite
    item can name a command that never ran.

    ``None`` means no text: a ``command_pattern`` does not match the call and an
    ``exclude_pattern`` does not exclude it.

    Rationale: .claude/notes/contracts.md § Which text a pattern searches
    """
    raw_command = cmd.parameters.get("command")
    if _is_shell_call(cmd):
        if not raw_command:
            return None
        if isinstance(raw_command, str):
            return raw_command, True
        # An argv LIST is searched as JSON; it never reaches shlex.
        return json.dumps(raw_command), False
    if tool_named:
        return json.dumps(cmd.parameters), False
    return None


def _unsearched_note(
    criterion: CommandExecutedCriterion, unsearched: list["CommandTelemetry"], match_count: int
) -> str:
    """A details line that says why calls that only name the command did not count, or ``""``.

    The hint to set ``tool_name`` is given only for a call that naming its tool makes searchable.
    """
    if not unsearched or match_count >= criterion.min_count:
        return ""
    hint = (
        " Set tool_name to search the params of another tool." if any(not _is_shell_call(c) for c in unsearched) else ""
    )
    return f"\n{len(unsearched)} call(s) without a command were not searched.{hint}"


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
    ) -> tuple[list[str], list["CommandTelemetry"]]:
        """Filter + label the commands matching this criterion.

        Shared by ``_check_impl`` and ``live_verdict`` so a command can never
        count for one and not the other (the live trigger and the authoritative
        score always agree on WHICH commands match). Callers compile the
        patterns and own error handling.

        Returns the labels of the matching commands, and the calls a
        ``command_pattern`` did not search because they gave no text (see
        :func:`_search_text`).
        """
        matching: list[str] = []
        unsearched: list[CommandTelemetry] = []
        for cmd in all_commands:
            if criterion.tool_name is not None and cmd.tool_name != criterion.tool_name:
                continue
            if criterion.require_success and cmd.result_status != "success":
                continue

            searched = _search_text(cmd, tool_named=criterion.tool_name is not None)
            # Raw text AND, for a shell command, its quote-resolved form, so neither
            # the pattern nor the exclusion can be dodged by quoting.
            haystacks = _match_haystacks(searched[0], is_shell=searched[1]) if searched else []
            if pattern is not None and not any(pattern.search(h) for h in haystacks):
                if searched is None:
                    unsearched.append(cmd)
                continue
            if exclude_re is not None and any(exclude_re.search(h) for h in haystacks):
                continue

            if searched is not None and searched[1]:
                matching.append(searched[0])
            else:
                matching.append(f"{cmd.tool_name}({json.dumps(cmd.parameters)[:80]})")
        return matching, unsearched

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

        match_count = len(self._matching_commands(criterion, all_commands, pattern, exclude_re)[0])
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
        matching_commands, unsearched = self._matching_commands(criterion, all_commands, pattern, exclude_re)

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
        details += _unsearched_note(criterion, unsearched, match_count)
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
