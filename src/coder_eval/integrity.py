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

# Characters that may precede `#` for it to start a comment (plus start-of-segment).
# A `#` glued to a word (`file#1.txt`) is part of the word, not a comment.
_COMMENT_BOUNDARY = " \t;|&(\n\r"

# Characters a bare (unquoted) heredoc delimiter word is made of.
_HEREDOC_DELIMITER_CHARS = re.compile(r"[\w.\-]")

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

# Utilities and shell keywords that touch a path without emitting its contents:
# file manipulation (`rm`, `mv`, `chmod`) and the loop / conditional keywords a
# segment can start with (`for f in check_*.py`). Without these, rule 7 reads an
# agent tidying up its own helper script as a leak and voids an honest row.
# `git` is deliberately NOT here -- it is classified per subcommand
# (:func:`_git_is_read`), because half of them print file content.
_NEUTRAL_UTILITIES = frozenset({"rm", "mv", "chmod", "for", "while", "if", "do", "done", "then", "fi"})

# `git` subcommands that do NOT emit file content: they stage, record, move or
# report. Everything else -- `show`, `cat-file -p`, `diff`, `blame`, `grep`,
# `log -p`, and any subcommand not listed here -- is treated as a read.
# `git show HEAD:<path>` prints a file as surely as `cat` does, so a blanket
# neutral `git` would be a one-command bypass for an agent that knows the detector
# exists. Unlisted subcommands default to READ for the same reason rule 7 does: a
# false positive is visible in the finding's evidence, a false negative is not.
_GIT_NEUTRAL_SUBCOMMANDS = frozenset(
    {
        "add",
        "commit",
        "status",
        "rm",
        "mv",
        "checkout",
        "restore",
        "stash",
        "init",
        "branch",
        "remote",
        "config",
        "clone",
        "fetch",
        "pull",
        "push",
        "tag",
        "reset",
        "clean",
    }
)

# `git` global options that take a separate value, so the token after them is not
# the subcommand.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})

# Flags that turn `git log` into a content-emitting command.
_GIT_PATCH_FLAGS = frozenset({"-p", "-u", "--patch"})

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

# Search-utility options that take a SEPARATE value operand. The token after one
# of these is data, not a flag: `grep -e -c file` searches FOR "-c". Keyed by
# utility because the same letter differs between tools (rg's `-E` takes an
# encoding, grep's takes nothing); utilities not listed use the grep set.
_GREP_VALUE_OPTIONS = frozenset(
    {"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "--regexp", "--file", "--max-count", "--include", "--exclude",
     "--exclude-dir", "--include-dir", "--label", "--binary-files", "--devices", "--directories"}
)
_SEARCH_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "grep": _GREP_VALUE_OPTIONS,
    "egrep": _GREP_VALUE_OPTIONS,
    "fgrep": _GREP_VALUE_OPTIONS,
    "rg": frozenset(
        {"-e", "-f", "-g", "-t", "-T", "-m", "-A", "-B", "-C", "-E", "-M", "-j", "-r", "--regexp", "--file",
         "--glob", "--iglob", "--type", "--type-not", "--type-add", "--max-count", "--max-columns", "--max-depth",
         "--max-filesize", "--context", "--after-context", "--before-context", "--encoding", "--threads", "--pre",
         "--pre-glob", "--replace", "--sort", "--sortr", "--colors", "--ignore-file"}
    ),
    "ag": frozenset({"-A", "-B", "-C", "-g", "-G", "-m", "--after", "--before", "--context", "--file-search-regex",
                     "--ignore", "--ignore-dir", "--max-count", "--pager", "--workers"}),
    "ack": frozenset({"-A", "-B", "-C", "-m", "-g", "--match", "--max-count", "--after-context", "--before-context",
                      "--context", "--type", "--ignore-dir", "--ignore-file", "--pager", "--output"}),
}

# Structured tools whose whole purpose is to return file content.
_READ_TOOLS = frozenset({"Read", "NotebookRead", "ReadFile", "read_file", "view", "View"})

# Structured tools that only enumerate paths.
_LISTING_TOOLS = frozenset({"Glob", "LS", "ListDir", "list_dir", "Ls", "glob", "TodoWrite", "Task", "Skill"})

# Structured tools that edit a file in place. Editing is a read: the tool requires
# the current content to locate what it replaces, and the agent had to have seen
# that content to write the edit.
_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "NotebookEdit"})

# Structured tools that produce content rather than consume it. `Write` names the
# file it creates -- including the deliverable a task asks for, whose name may BE
# graded material -- and `WebFetch` names a URL, not a local path.
_NEUTRAL_TOOLS = frozenset({"Write", "WebFetch"})

# Parameter keys that hold filesystem paths in the structured tools we know.
# Everything else a tool call carries is prose or a pattern, not a file it
# opens: `Grep(pattern="RESOLUTION.md", path="src")` searches FOR the name, and
# an Edit whose old_string quotes a graded name has read only the file at its
# file_path. `glob` counts as a path: in content mode it selects which files'
# lines are emitted.
_PATH_PARAMETER_KEYS = ("file_path", "path", "paths", "notebook_path", "glob")

# The structured tools whose parameter schema this module knows, and may
# therefore narrow to `_PATH_PARAMETER_KEYS`. An unrecognised tool keeps its
# full parameter text: its schema is unknown, and a hit there ends as
# INCONCLUSIVE rather than TAINTED, so over-matching is the safe direction.
_SCHEMA_KNOWN_TOOLS = _READ_TOOLS | _EDIT_TOOLS | _LISTING_TOOLS | _NEUTRAL_TOOLS | frozenset({"Grep"})

# Basename patterns that are graded material in every suite, independent of what
# this particular task declares. Deliberately short: each entry is a name the
# framework or the task-authoring convention owns. One of them (`RESOLUTION.md`)
# is ALSO the required deliverable of the troubleshoot suite -- the agent is
# supposed to write and re-read its own copy -- so basename-glob matches are
# excused when the matched path is one the agent itself created earlier in the
# transcript (:func:`_is_agent_created`); a golden elsewhere still matches.
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

    Three adjustments to ``fnmatch.translate``, which anchors the whole string:

    * The trailing ``\\Z`` anchor is dropped so the pattern can be SEARCHED for
      inside command text rather than matched against a bare filename.
    * The wildcard is narrowed so it cannot cross a path separator
      (``check_*.py`` must not match ``check_dir/other.py``).
    * Dropping the anchor also drops the filename's right-hand boundary, and
      ``translate`` never had a left-hand one, so both are restored as a
      lookbehind/lookahead pair. Without them ``RESOLUTION.md`` matches inside
      ``RESOLUTION.md.draft`` and ``task.yaml`` inside ``my-task.yaml.bak``, and
      an agent gets voided for reading its own scratch file.

    The boundary class is ``[\\w.-]`` on both sides -- exactly the characters a
    filename can continue through -- which is also what :func:`_segment_to_regex`
    uses. Everything a real reference is delimited BY is therefore still a match:
    string start/end, whitespace, ``/`` (so ``./RESOLUTION.md`` and
    ``../scen/RESOLUTION.md`` hit, and Windows backslashes too, since
    :func:`_normalize` has already turned them into ``/``), quotes, and shell
    metacharacters (``;``, ``|``, ``&``, ``)``, ``=``).
    """
    body = fnmatch.translate(_normalize(glob))
    # translate() emits `(?s:...)\Z`; strip the anchor and re-scope the wildcard.
    body = body.removesuffix(r"\Z")
    body = body.replace(".*", "[^/]*")
    return re.compile(r"(?<![\w.\-])" + body + r"(?![\w.\-])")


def _literal_to_regex(path: str, *, directory: bool = False) -> re.Pattern[str]:
    """Compile a literal (already-normalized) path into a whole-path pattern.

    A raw substring test lets ``reference.file=solution.py`` match the agent's
    own ``solution.py.bak``, and ``reference.directory=_reference`` match a
    sibling ``_reference_notes/`` -- both honest files, both voided under
    ``void``. A file therefore matches exactly, delimited by the same
    ``[\\w.-]`` filename-boundary class :func:`_glob_to_regex` uses; a directory
    matches exactly or continues with ``/`` into its contents, never into a
    longer name.
    """
    body = re.escape(path.rstrip("/") if directory else path)
    tail = r"(?=/|(?![\w.\-]))" if directory else r"(?![\w.\-])"
    return re.compile(r"(?<![\w.\-])" + body + tail)


def _segment_to_regex(segment: str) -> re.Pattern[str]:
    """Compile a path segment into a pattern that matches it as a path COMPONENT.

    ``m`` must match ``m/.store`` and ``<artifacts>/m/.store`` but not
    ``stream/x`` or ``m.json``, so the component is anchored between a leading
    boundary that rejects any character a path component could continue from and
    either its trailing separator OR the same boundary: ``rg . _fixtures`` and
    ``grep -R . mocks`` hand the whole protected directory to a recursive
    search without ever typing a ``/``, and whether that emits or merely lists
    is the utility's call, not the matcher's. A leading separator is NOT
    required either: agents open these paths relatively and quoted
    (``open('m/.store')``).
    """
    return re.compile(r"(?<![\w.\-])" + re.escape(_normalize(segment).strip("/")) + r"(?=/|(?![\w.\-]))")


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


# Characters that end a path token inside an already-normalized command string.
_TOKEN_DELIMITERS = " \t'\";|&<>()="


def _created_path(raw: str) -> str:
    """Normalize a path the agent created, for :func:`_is_agent_created` lookups."""
    path = _normalize(raw)
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_agent_created(haystack: str, start: int, end: int, created: frozenset[str] | set[str]) -> bool:
    """Whether the filename matched at ``[start, end)`` sits in a path the agent
    itself created earlier in this transcript.

    The basename globs are location-independent, and one of them names the
    troubleshoot suite's required DELIVERABLE: every honest agent writes
    ``RESOLUTION.md`` and -- because the harness enforces Read-before-Edit --
    reads it back. Those reads are the agent's own work, not a leak, so a glob
    match is excused when its whole path token is one the agent created.

    The comparison is one-directional on purpose: a RELATIVE read may resolve
    into a created path as its component-suffix (``cat RESOLUTION.md`` after
    ``Write /workspace/RESOLUTION.md``), but an ABSOLUTE read is never excused
    by a relative creation and a longer relative read never by a shorter one --
    otherwise writing your own ``RESOLUTION.md`` once would license reading
    every golden of the same name.
    """
    if not created:
        return False
    left = start
    while left > 0 and haystack[left - 1] not in _TOKEN_DELIMITERS:
        left -= 1
    right = end
    while right < len(haystack) and haystack[right] not in _TOKEN_DELIMITERS:
        right += 1
    token = haystack[left:right]
    while token.startswith("./"):
        token = token[2:]
    if token in created:
        return True
    is_relative = not token.startswith("/") and re.match(r"[a-z]:/", token) is None
    return is_relative and any(c.endswith("/" + token) for c in created)


def _find_match(text: str, spec: GradedMaterialSpec, created: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Return the graded-material reference found in ``text``, or None.

    Literal paths and directory prefixes are matched as whole paths on the
    normalized form (:func:`_literal_to_regex`); basename globs are regex-matched
    wherever they appear, EXCEPT on a path the agent itself created earlier
    (``created``, see :func:`_is_agent_created`) -- its own deliverable is not an
    answer key; path segments are matched as a path component so a
    relatively-typed ``../mocks/responses/manifest.json`` is caught too; grader
    scripts are matched last and only under the task directory
    (:func:`_grader_match`).
    """
    haystack = _normalize(text)

    for candidate in spec.paths:
        needle = _normalize(candidate)
        if needle and _literal_to_regex(needle).search(haystack):
            return candidate
    for candidate in spec.directories:
        needle = _normalize(candidate)
        if needle and _literal_to_regex(needle, directory=True).search(haystack):
            return candidate
    for glob in spec.basename_globs:
        for match in _glob_to_regex(glob).finditer(haystack):
            if not _is_agent_created(haystack, match.start(), match.end(), created):
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


def _git_subcommand(tokens: list[str]) -> str | None:
    """The subcommand of a ``git`` invocation, or None when none is identifiable.

    Global options are stepped over, including the ones that take a separate value
    (``git -C /repo show …``), so the returned token is the verb and not a path.
    """
    seen_git = False
    skip_value = False
    for token in tokens:
        name = Path(token.replace("\\", "/")).name.casefold().removesuffix(".exe")
        if not seen_git:
            seen_git = name == "git"
            continue
        if skip_value:
            skip_value = False
            continue
        if token in _GIT_VALUE_OPTIONS:
            skip_value = True
            continue
        if token.startswith("-"):
            continue
        return token.casefold()
    return None


def _git_is_read(tokens: list[str]) -> bool:
    """Whether a ``git`` invocation emits file CONTENT.

    ``git show HEAD:<path>`` and ``git cat-file -p`` print a file as surely as
    ``cat`` does, while ``git add`` / ``git status`` print nothing of it. ``git log``
    is the one subcommand that is both, decided by its patch flag. An unlisted
    subcommand -- and an invocation whose subcommand cannot be found at all -- counts
    as a read, matching rule 7: an unrecognised command holding a path to the answer
    key is more likely to be reading it than not.
    """
    subcommand = _git_subcommand(tokens)
    if subcommand is None:
        return True
    if subcommand == "log":
        return any(token in _GIT_PATCH_FLAGS for token in tokens)
    return subcommand not in _GIT_NEUTRAL_SUBCOMMANDS


def _strip_redirects(segment: str) -> tuple[str, list[str], list[str]]:
    """Remove real output redirects from a segment; collect the redirect targets.

    The old ``partition(">")`` treated ANY ``>`` as an output redirect -- one
    inside an awk program (``awk '$1 > 0' KEY``), a sed pattern, or a plain
    operand list (``cat > /tmp/copy KEY``) -- and wrote off everything after it
    as a write target, which made "put a ``>`` anywhere" a one-character bypass.
    A real redirect is an UNQUOTED operator and consumes exactly one word; every
    other operand is still passed to the utility.

    Returns ``(stripped, input_targets, created_targets)``: the segment with
    each output-redirect operator (``>``, ``>>``, ``>&``, ``&>``, fd-prefixed
    forms) and the single word it consumes removed; the target words of plain
    ``<`` input redirects (which the SHELL reads, whatever the utility is); and
    the targets of TRUNCATING output redirects (``>`` / ``&>``, not ``>>`` --
    an append leaves the original content readable, a truncation replaces it),
    which mark files the agent itself created. Heredoc operators (``<<``),
    here-strings (``<<<``) and process substitution (``<(…)``) are left in
    place: their text is data or an executing command, and either way it must
    stay visible to the caller's matching.
    """
    out: list[str] = []
    input_targets: list[str] = []
    created_targets: list[str] = []
    in_single = in_double = False
    i = 0
    n = len(segment)

    def _consume_word(j: int) -> tuple[str, int]:
        """Read one (possibly quoted) word starting at ``j``; return (word, end)."""
        while j < n and segment[j] in " \t":
            j += 1
        word: list[str] = []
        quote = ""
        while j < n:
            ch = segment[j]
            if quote:
                if ch == quote:
                    quote = ""
                else:
                    word.append(ch)
                j += 1
                continue
            if ch in "'\"":
                quote = ch
                j += 1
                continue
            if ch == "\\" and j + 1 < n:
                word.append(segment[j + 1])
                j += 2
                continue
            if ch in " \t<>|;&":
                break
            word.append(ch)
            j += 1
        return "".join(word), j

    while i < n:
        c = segment[i]
        if in_single:
            out.append(c)
            in_single = c != "'"
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(segment[i : i + 2])
            i += 2
            continue
        if in_double:
            out.append(c)
            in_double = c != '"'
            i += 1
            continue
        if c in "'\"":
            in_single = c == "'"
            in_double = c == '"'
            out.append(c)
            i += 1
            continue
        if c == ">" or segment[i : i + 2] == "&>":
            j = i + 1 if c == ">" else i + 2
            appending = j < n and segment[j] == ">"
            if appending:
                j += 1
            duplicating = j < n and segment[j] == "&"  # fd duplication: >&2, 2>&1
            if duplicating:
                j += 1
            word, j = _consume_word(j)
            if word and not appending and not duplicating:
                created_targets.append(word)
            out.append(" ")
            i = j
            continue
        if c == "<" and segment[i + 1 : i + 2] not in ("<", "("):
            word, j = _consume_word(i + 1)
            if word:
                input_targets.append(word)
            out.append(" ")
            i = j
            continue
        out.append(c)
        i += 1

    return "".join(out), input_targets, created_targets


def _search_is_files_only(utility: str, tokens: list[str]) -> bool:
    """Whether a grep/rg invocation reports only file names or match counts.

    Options are parsed, not swept: a token is only a flag when it sits in flag
    position. ``--`` ends option parsing (``grep -- '-c' KEY`` searches FOR the
    text "-c"), and an option that takes a separate value consumes the next
    token (``grep -e -c KEY`` searches for "-c" too). Anything after either is
    an operand, and an operand can never put the search into files-only mode.
    """
    value_options = _SEARCH_VALUE_OPTIONS.get(utility, _GREP_VALUE_OPTIONS)
    seen_utility = False
    expect_value = False
    for token in tokens:
        if not seen_utility:
            # Step over wrappers/assignments to the utility itself, mirroring
            # _segment_utility: its tokens are not search options.
            seen_utility = Path(token.replace("\\", "/")).name.casefold().removesuffix(".exe") == utility
            continue
        if expect_value:
            expect_value = False
            continue
        if token == "--":
            return False  # only operands remain, and none of the flags so far matched
        if token in _SEARCH_FILES_ONLY_LONG:
            return True
        if token in value_options:
            expect_value = True
            continue
        if token.startswith("--"):
            continue  # long flag (a `--opt=value` carries its value inline)
        if token.startswith("-") and len(token) > 1:
            # Bundled short flags: `-rl`, `-il`. A bundled value-taking option
            # consumes the rest of the bundle (or the next token) as its value.
            for position, char in enumerate(token[1:], start=1):
                if "-" + char in value_options:
                    expect_value = position == len(token) - 1
                    break
                if char in _SEARCH_FILES_ONLY_SHORT:
                    return True
    return False


def _classify_segment(
    segment: str, spec: GradedMaterialSpec, created: frozenset[str] | set[str] = frozenset()
) -> tuple[bool, str | None]:
    """Decide whether one shell segment READ graded material.

    Returns ``(is_read, matched_reference)``. ``matched_reference`` is set
    whenever the segment mentions graded material at all, so a caller can tell
    "mentioned but only listed" from "never mentioned".

    Rules, in order:

    1. No graded-material reference anywhere in the segment -> not a read. A
       basename-glob reference on a path the agent itself created earlier
       (``created``) does not count: that is its own deliverable, not a golden.
    2. A reference consumed by an input redirect (``< file``) -> a read, whatever
       the utility is; the shell does the reading.
    3. An output redirect (``>`` / ``>>`` / ``>&`` / ``&>``) consumes exactly one
       word; a reference that survives redirect stripping is an operand and still
       counts, while one appearing ONLY as a write target is the destination the
       agent is writing (``cat > check_env.py``) -> not a read. A quoted ``>``
       (inside an awk/sed program) is not a redirect at all.
    4. ``git`` -> decided by its subcommand (:func:`_git_is_read`): ``git show`` /
       ``cat-file`` / ``diff`` / ``blame`` / ``grep`` / ``log -p`` print content,
       ``git add`` / ``status`` / ``checkout`` do not.
    5. A leading search utility -> decided by its own flags: restricted to file
       names or counts is not a read, otherwise it is. Decided BEFORE the token
       sweep below, because a search utility's operands are patterns and paths,
       never nested executables -- ``grep -l cat KEY`` searches FOR "cat", it
       does not run it.
    6. Any content-emitting utility appearing as a token -> a read. Checked
       across all tokens, not just the leading one, so ``find … -exec cat {}``
       and ``xargs cat`` do not slip past on their wrapper's name.
    7. A listing/metadata utility, or a utility that moves, removes or otherwise
       manipulates a file without emitting it -> not a read.
    8. Anything else -> a read. Conservative on purpose: an unrecognised utility
       holding a path to the answer key is more likely a read than not, and a
       false positive is visible in the finding's evidence while a false negative
       is invisible.
    """
    matched = _find_match(segment, spec, created)
    if matched is None:
        return False, None

    utility, tokens = _segment_utility(segment)
    normalized_tokens = [Path(t.replace("\\", "/")).name.casefold().removesuffix(".exe") for t in tokens]

    stripped, input_targets, _ = _strip_redirects(segment)
    if any(_find_match(target, spec, created) is not None for target in input_targets):
        return True, matched

    if _find_match(stripped, spec, created) is None:
        # Every reference sits in an output-redirect target: a write, not a read.
        return False, matched

    # Before the token sweep below: `git`'s own subcommand is the authority on
    # whether it printed anything, so `git commit -m "cat the file"` is not a read
    # and `git show HEAD:<answer key>` is.
    if utility == "git":
        return _git_is_read(tokens), matched

    if utility in _SEARCH_UTILITIES:
        return not _search_is_files_only(utility, tokens), matched

    if any(name in _READ_UTILITIES for name in normalized_tokens):
        return True, matched

    if utility in _LISTING_UTILITIES or utility in _NEUTRAL_UTILITIES:
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
    # Paths the agent itself created, in transcript order: the required
    # deliverable shares a basename glob with the graded goldens, and only
    # provenance tells the agent's own copy apart (see _is_agent_created).
    created: set[str] = set()

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
                is_read, matched = _bash_read(text, spec, created)
            else:
                is_read, matched, understood = _structured_read(cmd, text, spec, created)
                if matched is not None and not understood:
                    unclassified_hits += 1
                    notes.append(
                        f"{cmd.tool_name} referenced {matched} but its read semantics are unknown; not counted"
                    )
                if cmd.tool_name == "Write" and isinstance(cmd.parameters.get("file_path"), str):
                    created.add(_created_path(cmd.parameters["file_path"]))

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


def _split_segments(command: str) -> list[str]:
    """Split a shell command into the segments that actually EXECUTE.

    A naive split on every ``;``/``|``/newline classifies inert data as commands:
    the argument of ``printf '%s\\n' 'harmless; cat KEY'``, the body of a heredoc,
    the tail of a comment. Each of those voids an honest row under ``void`` mode,
    so the split honors the three shell constructs that make text inert:

    * **Quoting.** ``'…'`` and ``"…"`` spans (and backslash escapes) never
      separate; their content stays inside the enclosing segment, where the
      leading utility's semantics decide what it means.
    * **Comments.** An unquoted ``#`` opening a word discards the rest of the
      line -- commented-out text never ran.
    * **Heredocs.** A ``<<'EOF'``-style QUOTED delimiter makes the body pure
      data, so those lines are dropped. An unquoted delimiter (``<<EOF``) leaves
      expansions live -- ``$(cat KEY)`` inside the body executes -- so those
      lines stay scanned as segments: a false positive there is visible in the
      finding's evidence, a missed substitution is not.

    Separators are the unquoted operators ``&&``, ``||``, ``|``, ``;`` and line
    breaks -- the same set the old regex split on, minus everything quoted.
    """
    segments: list[str] = []
    current: list[str] = []
    pending_heredocs: list[tuple[str, bool]] = []  # (delimiter, delimiter_was_quoted)
    in_single = in_double = False
    prev = ""  # last significant char, "" at a segment boundary
    i = 0
    n = len(command)

    def _flush() -> None:
        nonlocal prev
        text = "".join(current)
        current.clear()
        prev = ""
        if text.strip():
            segments.append(text)

    while i < n:
        c = command[i]
        if in_single:
            current.append(c)
            prev = c
            in_single = c != "'"
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            current.append(command[i : i + 2])
            prev = command[i + 1]
            i += 2
            continue
        if in_double:
            current.append(c)
            prev = c
            in_double = c != '"'
            i += 1
            continue
        if c in "'\"":
            in_single = c == "'"
            in_double = c == '"'
            current.append(c)
            prev = c
            i += 1
            continue
        if c == "#" and (prev == "" or prev in _COMMENT_BOUNDARY):
            newline = command.find("\n", i)
            i = n if newline == -1 else newline  # leave the newline for the branch below
            continue
        if c == "<" and command[i : i + 2] == "<<" and command[i : i + 3] != "<<<":
            # Heredoc operator: record the delimiter and whether it was quoted;
            # the body starts after the next unquoted newline.
            j = i + 2
            if j < n and command[j] == "-":
                j += 1
            while j < n and command[j] in " \t":
                j += 1
            quoted = False
            delimiter_chars: list[str] = []
            if j < n and command[j] in "'\"":
                quote = command[j]
                quoted = True
                j += 1
                while j < n and command[j] != quote:
                    delimiter_chars.append(command[j])
                    j += 1
                j += 1  # closing quote
            elif j < n and command[j] == "\\":
                quoted = True
                j += 1
                while j < n and _HEREDOC_DELIMITER_CHARS.match(command[j]):
                    delimiter_chars.append(command[j])
                    j += 1
            else:
                while j < n and _HEREDOC_DELIMITER_CHARS.match(command[j]):
                    delimiter_chars.append(command[j])
                    j += 1
            if delimiter_chars:
                pending_heredocs.append(("".join(delimiter_chars), quoted))
                current.append(command[i:j])
                prev = command[j - 1]
                i = j
                continue
            # `<<` with no delimiter: fall through and treat it as ordinary text.
        if c in "\n\r":
            _flush()
            i += 1
            while pending_heredocs and i < n:
                delimiter, quoted = pending_heredocs.pop(0)
                while i < n:
                    end = command.find("\n", i)
                    end = n if end == -1 else end
                    line = command[i:end]
                    i = end + 1
                    if line.strip() == delimiter:
                        break
                    if not quoted and line.strip():
                        segments.append(line)
            continue
        if command[i : i + 2] in ("&&", "||"):
            _flush()
            i += 2
            continue
        if c in ";|":
            _flush()
            i += 1
            continue
        current.append(c)
        prev = c
        i += 1

    _flush()
    return segments


def _bash_read(command: str, spec: GradedMaterialSpec, created: set[str] | None = None) -> tuple[bool, str | None]:
    """Classify a shell command by splitting it into segments and judging each.

    ``created`` is the transcript-ordered set of paths the agent has written so
    far; each segment's truncating redirect targets are added AFTER the segment
    is classified, so ``cat > RESOLUTION.md && cat RESOLUTION.md`` excuses the
    re-read while ``sed … golden > RESOLUTION.md`` still counts the read that
    produced the file.
    """
    created = set() if created is None else created
    mentioned: str | None = None
    for segment in _split_segments(command):
        is_read, matched = _classify_segment(segment, spec, created)
        if matched is not None:
            mentioned = matched
        if is_read:
            return True, matched
        _, _, made = _strip_redirects(segment)
        created.update(_created_path(target) for target in made)
    return False, mentioned


def _structured_read(
    cmd: CommandTelemetry,
    text: str,
    spec: GradedMaterialSpec,
    created: frozenset[str] | set[str] = frozenset(),
) -> tuple[bool, str | None, bool]:
    """Classify a non-Bash tool call.

    Returns ``(is_read, matched, semantics_understood)``. Unlike a shell string, a
    structured tool has fixed semantics, so the decision is by tool name rather
    than by heuristic -- and for a tool whose schema is known, only its path
    parameters are matched (``_PATH_PARAMETER_KEYS``): patterns, replacement
    text and mode switches name graded material without touching it. A tool this
    module does not recognise keeps its full parameter text and gets
    ``semantics_understood=False`` when it touched graded material, which the
    caller turns into INCONCLUSIVE -- neither a silent pass nor a taint on a tool
    whose behavior we are guessing at.
    """
    if cmd.tool_name in _SCHEMA_KNOWN_TOOLS:
        haystack = " ".join(str(cmd.parameters[key]) for key in _PATH_PARAMETER_KEYS if cmd.parameters.get(key))
    else:
        haystack = text
    matched = _find_match(haystack, spec, created)
    if matched is None:
        return False, None, True

    if cmd.tool_name in _READ_TOOLS or cmd.tool_name in _EDIT_TOOLS:
        return True, matched, True
    if cmd.tool_name in _LISTING_TOOLS or cmd.tool_name in _NEUTRAL_TOOLS:
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
