"""Run-integrity detection: did the agent read graded material instead of evidence?

A task's score is only a measurement of the agent if the agent worked from the
evidence the scenario intended. When it instead opens the reference solution, the
checker script, or its own task definition, a high score measures the leak. Those
rows are worse than useless: they inflate a suite average and hide a real
regression, and nothing in the run record distinguishes them.

This module answers one question over a finished transcript -- "did any command
read graded material?" -- and reports it as an :class:`IntegrityInfo`. It decides
nothing about the row's status; :mod:`coder_eval.orchestrator` owns that gate.

Two design constraints are load-bearing:

* **Detection, not containment.** Under ``driver: tempdir`` the whole checkout is
  on the same filesystem as the agent and unrestricted access is intentional
  (see ``sandbox.py``), so there is no mount to take away. Detection is the only
  lever available on that driver, and it is driver-independent.
* **Scan the untruncated command.** ``CommandExecutedChecker`` clips command text
  at 2000 characters as a ReDoS guard -- exactly where a long ``cat`` hides. This
  module reads ``CommandTelemetry.parameters`` directly and never truncates the
  haystack.

The shell classifier (:func:`_classify_segment`) is the false-positive control and
the whole game on Codex, where every file read arrives as a ``Bash`` command
rather than a ``Read`` tool call. A directory listing that prints a path is not a
leak; a ``cat`` of the same path is.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from coder_eval.models import (
    CONTAINER_INPUT_DIR,
    CONTAINER_TASK_DIR,
    IntegrityFinding,
    IntegrityFindingKind,
    IntegrityInfo,
    IntegrityMode,
    IntegrityVerdict,
)


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition, TurnRecord
    from coder_eval.models.telemetry import CommandTelemetry


logger = logging.getLogger(__name__)


# Fraction of scanned commands whose parameters may be empty before the scan is
# treated as partially blind. Codex returns {} for tool kinds it does not model
# (`_tool_parameters`), so a small number is normal; a large one means the scan
# reported on a transcript it could not read, and CLEAN would be a lie.
_MAX_BLIND_COMMAND_RATIO = 0.10

# Characters of matched command text kept on a finding. Enough to see an obvious
# false positive without opening task.json; short enough that fifty findings do
# not bloat the row.
_MAX_EVIDENCE_CHARS = 240

# Shell operators that end one command and begin another. `||` precedes `|` so
# regex alternation consumes the two-character form first.
_SEGMENT_SEPARATOR = re.compile(r"\|\||&&|;|\||\n|\r")

# Wrappers that delegate to the real utility; skipped when finding the utility
# that decides a segment's classification.
_TRANSPARENT_PREFIXES = frozenset({"sudo", "env", "command", "time", "nohup", "nice", "exec", "builtin", "eval"})

# Utilities that only report a path's existence, name, or metadata. A hit inside
# one of these is not a read -- an `ls` that prints `RESOLUTION.md` tells the
# agent nothing it could put in an answer.
_LISTING_UTILITIES = frozenset(
    {
        "ls",
        "dir",
        "find",
        "fd",
        "tree",
        "stat",
        "file",
        "wc",
        "du",
        "test",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "echo",
        "printf",
        "pwd",
        "which",
        "type",
        "mkdir",
        "touch",
        "cd",
    }
)

# Utilities that emit file CONTENT. A hit inside one of these is a read.
_READ_UTILITIES = frozenset(
    {
        "cat",
        "bat",
        "tac",
        "head",
        "tail",
        "sed",
        "awk",
        "less",
        "more",
        "strings",
        "od",
        "xxd",
        "hexdump",
        "base64",
        "zcat",
        "gunzip",
        "unzip",
        "tar",
        "python",
        "python3",
        "py",
        "node",
        "perl",
        "ruby",
        "jq",
        "yq",
        "diff",
        "cmp",
        "cp",
        "copy",
        "tee",
        "sort",
        "uniq",
        "cut",
        "tr",
        "nl",
        "open",
        "source",
        "vim",
        "nano",
        "emacs",
    }
)

# Search utilities: a read unless restricted to reporting which files matched.
_SEARCH_UTILITIES = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "select-string"})

# Flags that make a search utility report file names or counts instead of the
# matching lines. `-c` and `-l` may be bundled (`-rl`), so short flags are also
# matched character-wise.
_SEARCH_FILES_ONLY_LONG = frozenset(
    {"--files", "--files-with-matches", "--files-without-match", "--count", "-l", "-L", "-c"}
)
_SEARCH_FILES_ONLY_SHORT = frozenset({"l", "L", "c"})

# Structured tools whose whole purpose is to return file content.
_READ_TOOLS = frozenset({"Read", "NotebookRead", "ReadFile", "read_file", "view", "View"})

# Structured tools that only enumerate paths.
_LISTING_TOOLS = frozenset({"Glob", "LS", "ListDir", "list_dir", "Ls", "glob", "TodoWrite", "Task", "Skill"})

# Basename patterns that are graded material in every suite, independent of what
# this particular task declares. Deliberately short: each entry is a name the
# framework or the task-authoring convention owns, never a name an agent's own
# work would produce.
_GRADED_BASENAME_GLOBS = ("RESOLUTION.md", "*.expected", "task.yaml", "context.json")

# Grader-script names. `check.py` (no underscore) grades live tasks too, so the
# underscore cannot be required -- but neither name is the task's alone: `check.py`
# is also perfectly ordinary application code. These are therefore matched only
# when the path locates them in the task's own directory (:func:`_grader_match`),
# unlike the basename globs above.
_GRADER_SCRIPT_GLOBS = ("check_*.py", "check.py")

# Directory markers that put a grader script in the task's own directory: the
# suite layout every task YAML lives under, and both spellings of the framework's
# task-dir variable. The resolved task directory is added per task.
_GRADER_DIR_MARKERS = ("tests/tasks/", "$task_dir/", f"{CONTAINER_TASK_DIR}/")

# Path SEGMENTS that hold answer keys wherever they appear. Segments rather than
# resolved prefixes because this is how an agent types them -- `cat
# _fixtures/expected/foo.json`, `../_fixtures/solution/main.py` -- so an absolute
# prefix derived on the host matches neither spelling.
_GRADED_PATH_SEGMENTS = ("_fixtures",)

# Path segments holding the scenario's mock/fixture DATA: the recorded responses a
# mock shim replays and the shim itself. Reported as MOCK_DATA_READ rather than
# GRADED_READ -- reading the fixture store is a different act from reading the
# reference answer, and the first rollout runs record-only, so the two have to be
# triageable apart.
_MOCK_PATH_SEGMENTS = ("mocks", "mock_src")


@dataclass(frozen=True)
class GradedMaterialSpec:
    """What counts as graded material for one task.

    Derived from what the harness already knows -- the task file, its declared
    reference, the mock/fixture directories it declares, the ``$TASK_DIR``
    operands its own criteria use, and the framework's container mounts -- rather
    than from hardcoded suite paths, so it stays correct as suites are added and
    renamed.

    Two match shapes, because agents type two shapes: ``paths`` / ``directories``
    are resolved and substring-matched, while ``path_segments`` /
    ``mock_segments`` are path COMPONENTS matched wherever they occur, so a
    relative ``../mocks/responses/manifest.json`` is caught as well.
    """

    paths: frozenset[str] = field(default_factory=frozenset)
    """Literal file paths (task YAML, reference file, criterion operands)."""

    directories: frozenset[str] = field(default_factory=frozenset)
    """Directory prefixes (reference directory, framework input mount)."""

    basename_globs: tuple[str, ...] = ()
    """Filename patterns that are graded material regardless of location."""

    path_segments: frozenset[str] = field(default_factory=frozenset)
    """Answer-key path components (``_fixtures``), matched anywhere in a path."""

    mock_segments: frozenset[str] = field(default_factory=frozenset)
    """Mock/fixture-store path components (declared mock dirs, staged fixture
    mount points), matched anywhere in a path and reported as MOCK_DATA_READ."""

    grader_globs: tuple[str, ...] = ()
    """Grader-script patterns, matched only under the task directory."""

    task_dir: str | None = None
    """The task's own directory, when known -- the location a grader-script match
    must carry to count."""

    def is_empty(self) -> bool:
        """Whether the spec would match nothing at all."""
        return not (
            self.paths
            or self.directories
            or self.basename_globs
            or self.path_segments
            or self.mock_segments
            or self.grader_globs
        )


def _normalize(text: str) -> str:
    """Case-fold and forward-slash a path or command for substring comparison.

    Windows task files arrive with backslashes while the sandbox command that
    reads them uses forward slashes (Git Bash), so a raw comparison misses.
    """
    return text.replace("\\", "/").casefold()


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a basename glob into a pattern that matches it inside a command.

    ``fnmatch.translate`` alone anchors the whole string; the wildcard is also
    narrowed so it cannot cross a path separator (``check_*.py`` must not match
    ``check_dir/other.py``).
    """
    body = fnmatch.translate(_normalize(glob))
    # translate() emits `(?s:...)\Z`; strip the anchor and re-scope the wildcard.
    body = body.removesuffix(r"\Z")
    body = body.replace(".*", "[^/]*")
    return re.compile(body)


def _segment_to_regex(segment: str) -> re.Pattern[str]:
    """Compile a path segment into a pattern that matches it as a path COMPONENT.

    ``m`` must match ``m/.store`` and ``<artifacts>/m/.store`` but not
    ``stream/x``, so the component is anchored on its trailing separator plus a
    leading boundary that rejects any character a path component could continue
    from. A leading separator is NOT required: agents open these paths relatively
    and quoted (``open('m/.store')``).
    """
    return re.compile(r"(?<![\w.\-])" + re.escape(_normalize(segment).strip("/")) + "/")


def _path_segments(raw: str) -> set[str]:
    """Normalize a declared sandbox-relative directory into matchable segments.

    Returns the declared path itself plus its root component: a task that declares
    ``mocks/bin`` as its shim directory still keeps the recorded responses it
    replays under ``mocks/``. ``.`` (the sandbox root) yields nothing -- every
    read would match it.
    """
    parts = [p for p in _normalize(raw).split("/") if p not in ("", ".")]
    if not parts:
        return set()
    return {"/".join(parts), parts[0]}


def _declared_mock_segments(task: TaskDefinition) -> set[str]:
    """Mock/fixture directories this task DECLARES, as path segments.

    Two declarations locate a scenario's fixture store: ``sandbox.mock_path_dirs``
    (the directories whose contents the harness makes executable and prepends to
    the agent's PATH -- the shims) and ``template_sources[*].mount_point`` (where a
    staged tree lands). A mount point is only as precise as the task made it: one
    that points at the agent's own working tree widens the spec to that tree, which
    is why these are reported as MOCK_DATA_READ and not folded into GRADED_READ.
    """
    segments: set[str] = set()
    for raw in task.sandbox.mock_path_dirs or []:
        segments.update(_path_segments(raw))
    for source in task.sandbox.template_sources or []:
        mount_point = getattr(source, "mount_point", None)
        if mount_point:
            segments.update(_path_segments(mount_point))
    return segments


def derive_graded_material(task: TaskDefinition, task_file: Path | None) -> GradedMaterialSpec:
    """Work out this task's graded material from the harness's own configuration.

    Args:
        task: The resolved task definition (its ``reference``, ``pre_run``,
            ``post_run`` and ``run_command`` criteria are all read).
        task_file: Path to the task YAML, when the caller tracked it. Without it
            the task-relative paths (the YAML itself, ``reference.file``) cannot
            be resolved and only the location-independent patterns apply.

    Returns:
        The spec :func:`scan_commands` matches against.
    """
    paths: set[str] = set()
    directories: set[str] = {CONTAINER_INPUT_DIR}
    task_dir = task_file.parent if task_file is not None else None

    if task_file is not None:
        paths.add(str(task_file))
        base = task_file.parent
        reference = task.reference
        if reference is not None:
            if reference.file:
                paths.add(str(base / reference.file))
            if reference.directory:
                directories.add(str(base / reference.directory))

    # Operands the task's OWN criteria reach for. `python3 $TASK_DIR/check_x.py`
    # names the grader; an agent that runs the grader is grading itself.
    criterion_commands = [getattr(c, "command", "") for c in task.success_criteria]
    hook_commands = [c.command for c in (*task.pre_run, *task.post_run)]
    for command in (*criterion_commands, *hook_commands):
        paths.update(_task_dir_operands(command or "", task_dir))

    # Mock/fixture stores. Declared per task (mock dirs, staged mount points) plus
    # the two conventions no task spells out: `_fixtures/` holds golden solutions
    # and `mock_src/` the fixture sources.
    mock_segments = _declared_mock_segments(task) | set(_MOCK_PATH_SEGMENTS)
    path_segments = set(_GRADED_PATH_SEGMENTS)

    return GradedMaterialSpec(
        paths=frozenset(paths),
        directories=frozenset(directories),
        basename_globs=_GRADED_BASENAME_GLOBS,
        path_segments=frozenset(path_segments),
        mock_segments=frozenset(mock_segments - path_segments),
        grader_globs=_GRADER_SCRIPT_GLOBS,
        task_dir=str(task_dir) if task_dir is not None else None,
    )


def _task_dir_operands(command: str, task_dir: Path | None = None) -> set[str]:
    """Extract ``$TASK_DIR``-rooted operands from a framework-run command.

    Three spellings of the same read, because which one the agent types depends on
    the driver: the raw form (``$TASK_DIR/check_x.py``, what an agent that
    discovered the variable would use), the container-resolved form
    (``/work/task_dir/check_x.py``), and -- when the task directory is known -- the
    real on-disk path. Under ``driver: tempdir`` the task lives in the host
    checkout and the agent reads THAT path, which neither symbolic form matches.
    """
    operands: set[str] = set()
    for match in re.finditer(r"\$\{?TASK_DIR\}?(/[^\s'\";|&)]+)", command):
        suffix = match.group(1)
        operands.add(f"$TASK_DIR{suffix}")
        operands.add(f"{CONTAINER_TASK_DIR}{suffix}")
        if task_dir is not None:
            operands.add(str((task_dir / suffix.lstrip("/")).resolve()))
    return operands


def _find_match(text: str, spec: GradedMaterialSpec) -> str | None:
    """Return the graded-material reference found in ``text``, or None.

    Literal paths and directory prefixes are substring-matched on the normalized
    form; basename globs are regex-matched wherever they appear; path segments are
    matched as a path component so a relatively-typed
    ``../mocks/responses/manifest.json`` is caught too; grader scripts are matched
    last and only under the task directory (:func:`_grader_match`).
    """
    haystack = _normalize(text)

    for candidate in spec.paths:
        needle = _normalize(candidate)
        if needle and needle in haystack:
            return candidate
    for candidate in spec.directories:
        needle = _normalize(candidate)
        if needle and needle in haystack:
            return candidate
    for glob in spec.basename_globs:
        if _glob_to_regex(glob).search(haystack):
            return glob
    for segment in (*spec.path_segments, *spec.mock_segments):
        if _segment_to_regex(segment).search(haystack):
            return segment
    return _grader_match(haystack, spec)


def _grader_match(haystack: str, spec: GradedMaterialSpec) -> str | None:
    """Find a grader-script reference that is rooted in the task's OWN directory.

    ``check_env.py`` in the agent's working directory is the agent's own helper and
    ``check.py`` is ordinary application code, so a bare basename proves nothing: a
    grader-glob match only counts when the path it sits in names the task directory
    (its resolved path, either ``$TASK_DIR`` spelling, or the ``tests/tasks/``
    segment the suite layout puts on every task path).

    Args:
        haystack: Already-normalized command text.
        spec: The task's graded material.

    Returns:
        The matched glob, or None.
    """
    if not spec.grader_globs:
        return None

    markers = list(_GRADER_DIR_MARKERS)
    if spec.task_dir:
        markers.append(_normalize(spec.task_dir).rstrip("/") + "/")

    for glob in spec.grader_globs:
        # The directory component is bounded by the token: it cannot run over
        # whitespace or a quote into a neighbouring argument.
        pattern = re.compile(r"(?P<dir>[^\s'\"|;&()]*/)" + _glob_to_regex(glob).pattern)
        for match in pattern.finditer(haystack):
            if any(marker in match.group("dir") for marker in markers):
                return glob
    return None


def _finding_kind(matched: str, spec: GradedMaterialSpec) -> IntegrityFindingKind:
    """Which class of finding a matched reference produces.

    Only the mock/fixture-store segments are MOCK_DATA_READ; everything else --
    the reference solution, the task YAML, the grader, a golden solution under
    ``_fixtures/`` -- is a read of the answer key itself.
    """
    if matched in spec.mock_segments:
        return IntegrityFindingKind.MOCK_DATA_READ
    return IntegrityFindingKind.GRADED_READ


def _segment_utility(segment: str) -> tuple[str, list[str]]:
    """Leading utility of a shell segment (basename, lowercased) and its tokens.

    Leading ``VAR=value`` assignments and transparent wrappers (``sudo``, ``env``,
    ``time``, …) are stepped over so ``sudo cat x`` classifies as ``cat``.
    Returns ``("", tokens)`` when no utility can be identified.
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace splitting rather than
        # skipping the segment, since an unparseable command still ran.
        tokens = segment.split()

    for token in tokens:
        if "=" in token and not token.startswith(("-", "/", ".")) and token.split("=", 1)[0].isidentifier():
            continue  # leading environment assignment
        name = Path(token.replace("\\", "/")).name.casefold().removesuffix(".exe")
        if name in _TRANSPARENT_PREFIXES:
            continue
        return name, tokens
    return "", tokens


def _search_is_files_only(tokens: list[str]) -> bool:
    """Whether a grep/rg invocation reports only file names or match counts."""
    for token in tokens:
        if token in _SEARCH_FILES_ONLY_LONG:
            return True
        # Bundled short flags: `-rl`, `-il`. A lone `-` or a long flag is skipped.
        if (
            token.startswith("-")
            and not token.startswith("--")
            and any(c in _SEARCH_FILES_ONLY_SHORT for c in token[1:])
        ):
            return True
    return False


def _classify_segment(segment: str, spec: GradedMaterialSpec) -> tuple[bool, str | None]:
    """Decide whether one shell segment READ graded material.

    Returns ``(is_read, matched_reference)``. ``matched_reference`` is set
    whenever the segment mentions graded material at all, so a caller can tell
    "mentioned but only listed" from "never mentioned".

    Rules, in order:

    1. No graded-material reference anywhere in the segment -> not a read.
    2. A reference after an input redirect (``< file``) -> a read, whatever the
       utility is; the shell does the reading.
    3. Any content-emitting utility appearing as a token -> a read. Checked
       across all tokens, not just the leading one, so ``find … -exec cat {}``
       and ``xargs cat`` do not slip past on their wrapper's name.
    4. A search utility restricted to file names or counts -> not a read;
       otherwise a read.
    5. A pure listing/metadata utility -> not a read.
    6. Anything else -> a read. Conservative on purpose: an unrecognised utility
       holding a path to the answer key is more likely a read than not, and a
       false positive is visible in the finding's evidence while a false negative
       is invisible.
    """
    matched = _find_match(segment, spec)
    if matched is None:
        return False, None

    utility, tokens = _segment_utility(segment)
    normalized_tokens = [Path(t.replace("\\", "/")).name.casefold().removesuffix(".exe") for t in tokens]

    if "<" in segment:
        _, _, after = segment.partition("<")
        if _find_match(after, spec) is not None:
            return True, matched

    if any(name in _READ_UTILITIES for name in normalized_tokens):
        return True, matched

    if utility in _SEARCH_UTILITIES:
        return not _search_is_files_only(tokens), matched

    if utility in _LISTING_UTILITIES:
        return False, matched

    return True, matched


def _excerpt(text: str, matched: str) -> str:
    """A short window of ``text`` around ``matched``, for the finding's evidence."""
    flat = " ".join(text.split())
    index = _normalize(flat).find(_normalize(matched))
    if index < 0 or len(flat) <= _MAX_EVIDENCE_CHARS:
        return flat[:_MAX_EVIDENCE_CHARS]
    start = max(0, index - _MAX_EVIDENCE_CHARS // 3)
    return flat[start : start + _MAX_EVIDENCE_CHARS]


def _command_text(cmd: CommandTelemetry) -> str:
    """The scannable text of a command: the shell string, or its parameter values.

    ``result_summary`` is deliberately excluded. Result bodies are full of paths
    the agent merely saw printed, which is precisely the false-positive class the
    Antigravity backend already strips result-only keys to avoid.
    """
    if cmd.tool_name == "Bash" and isinstance(cmd.parameters.get("command"), str):
        return cmd.parameters["command"]
    return " ".join(str(v) for v in cmd.parameters.values())


def scan_commands(turns: list[TurnRecord], spec: GradedMaterialSpec) -> IntegrityInfo:
    """Scan a finished transcript for reads of graded material.

    Populates findings and the blind-spot counters, and derives the verdict from
    both: a positive hit is TAINTED even on a partially-visible transcript (going
    blind cannot un-see a hit), while a clean scan over a transcript the scanner
    could not fully read is INCONCLUSIVE rather than CLEAN.

    Args:
        turns: The run's iterations, in order.
        spec: Graded material for this task (see :func:`derive_graded_material`).

    Returns:
        An :class:`IntegrityInfo` with ``mode`` left at its default -- the caller
        stamps the mode it ran under and owns the gate.
    """
    findings: list[IntegrityFinding] = []
    notes: list[str] = []
    scanned = 0
    blind = 0
    unclassified_hits = 0

    for turn in turns:
        for index, cmd in enumerate(turn.commands):
            scanned += 1
            if not cmd.parameters:
                blind += 1
                continue

            text = _command_text(cmd)
            if not text:
                blind += 1
                continue

            if cmd.tool_name == "Bash":
                is_read, matched = _bash_read(text, spec)
            else:
                is_read, matched, understood = _structured_read(cmd, text, spec)
                if matched is not None and not understood:
                    unclassified_hits += 1
                    notes.append(
                        f"{cmd.tool_name} referenced {matched} but its read semantics are unknown; not counted"
                    )

            if is_read and matched is not None:
                kind = _finding_kind(matched, spec)
                subject = "mock fixture data" if kind is IntegrityFindingKind.MOCK_DATA_READ else "graded material"
                findings.append(
                    IntegrityFinding(
                        kind=kind,
                        detail=f"{cmd.tool_name} read {subject} ({matched})",
                        iteration=turn.iteration,
                        command_index=index,
                        tool_name=cmd.tool_name,
                        evidence=_excerpt(text, matched),
                    )
                )

    unrecovered = sum(t.unrecovered_subagent_threads for t in turns)
    if unrecovered:
        notes.append(f"{unrecovered} sub-agent(s) contributed no recovered tool calls; their commands were not scanned")

    blind_ratio = (blind / scanned) if scanned else 0.0
    too_blind = blind_ratio > _MAX_BLIND_COMMAND_RATIO
    if too_blind:
        notes.append(f"{blind} of {scanned} commands had no scannable parameters ({blind_ratio:.0%})")

    if findings:
        verdict = IntegrityVerdict.TAINTED
    elif unrecovered or too_blind or unclassified_hits:
        verdict = IntegrityVerdict.INCONCLUSIVE
    else:
        verdict = IntegrityVerdict.CLEAN

    return IntegrityInfo(
        verdict=verdict,
        findings=findings,
        commands_scanned=scanned,
        commands_without_parameters=blind,
        subagent_recovery_incomplete=bool(unrecovered),
        notes=notes,
    )


def _bash_read(command: str, spec: GradedMaterialSpec) -> tuple[bool, str | None]:
    """Classify a shell command by splitting it into segments and judging each."""
    mentioned: str | None = None
    for segment in _SEGMENT_SEPARATOR.split(command):
        if not segment.strip():
            continue
        is_read, matched = _classify_segment(segment, spec)
        if matched is not None:
            mentioned = matched
        if is_read:
            return True, matched
    return False, mentioned


def _structured_read(cmd: CommandTelemetry, text: str, spec: GradedMaterialSpec) -> tuple[bool, str | None, bool]:
    """Classify a non-Bash tool call.

    Returns ``(is_read, matched, semantics_understood)``. Unlike a shell string, a
    structured tool has fixed semantics, so the decision is by tool name rather
    than by heuristic. A tool this module does not recognise gets
    ``semantics_understood=False`` when it touched graded material, which the
    caller turns into INCONCLUSIVE -- neither a silent pass nor a taint on a tool
    whose behavior we are guessing at.
    """
    matched = _find_match(text, spec)
    if matched is None:
        return False, None, True

    if cmd.tool_name in _READ_TOOLS:
        return True, matched, True
    if cmd.tool_name in _LISTING_TOOLS:
        return False, matched, True
    if cmd.tool_name == "Grep":
        # Claude's Grep returns matching LINES only in content mode; the default
        # (`files_with_matches`) and `count` report where matches are, not what.
        output_mode = str(cmd.parameters.get("output_mode") or "files_with_matches")
        has_context = any(cmd.parameters.get(k) for k in ("-A", "-B", "-C"))
        return output_mode == "content" or has_context, matched, True
    return False, matched, False


def evaluate_integrity(
    task: TaskDefinition,
    task_file: Path | None,
    turns: list[TurnRecord],
    *,
    mode: IntegrityMode,
) -> IntegrityInfo:
    """Run the integrity pass for one task and return its verdict.

    Never raises: an integrity bug must not take down a row that otherwise ran
    fine, so an unexpected failure is reported as INCONCLUSIVE with the reason in
    ``notes``.

    Args:
        task: The resolved task definition.
        task_file: Path to the task YAML, when known.
        turns: The run's iterations.
        mode: The ``INTEGRITY_MODE`` in force. ``OFF`` short-circuits to SKIPPED.

    Returns:
        A populated :class:`IntegrityInfo` with ``mode`` stamped. ``voided`` is
        left False -- only the gate sets it.
    """
    if mode is IntegrityMode.OFF:
        return IntegrityInfo(verdict=IntegrityVerdict.SKIPPED, mode=mode, notes=["INTEGRITY_MODE=off"])

    try:
        spec = derive_graded_material(task, task_file)
        if spec.is_empty():
            return IntegrityInfo(
                verdict=IntegrityVerdict.SKIPPED,
                mode=mode,
                notes=["no graded material could be derived for this task"],
            )
        info = scan_commands(turns, spec)
    except Exception as exc:
        logger.warning("Integrity scan failed for task %s: %s", task.task_id, exc, exc_info=True)
        return IntegrityInfo(
            verdict=IntegrityVerdict.INCONCLUSIVE,
            mode=mode,
            notes=[f"integrity scan raised {type(exc).__name__}: {exc}"],
        )

    info.mode = mode
    return info
