"""Check the docstring and comment prose rules in ``src/coder_eval`` and ``tests`` (``_ROOTS``).

Two per-file rules, with no baseline: no docstring over 150 prose words (Typer commands
and ``@abstractmethod`` exempt), and own-line comments within a SHARE OF THE FILE'S
LENGTH. The house style is *no new essays*, not *no new documentation*. The report also
lists words in comment runs of three or more lines, for orientation only.

Also resolves every ``Rationale: <path> § <heading>`` pointer, and — under
``--assert-code-unchanged <ref>`` — proves a commit moved prose only, by comparing the
docstring-stripped AST and the multiset of functional directive comments against ``ref``.

Stdlib only, and it never imports, execs or evals a file it measures.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import textwrap
import tokenize
from collections import Counter
from pathlib import Path
from typing import NamedTuple


_DOCSTRING_ESSAY_WORDS = 150
_COMMENT_BLOCK_LINES = 3
# The longest own-line comment RUN a file may carry, and the blank lines a run
# reads through. A cap on the block, not on the file's total: the shape a comment
# may not take is a PARAGRAPH, and a file owes no allowance for one-line notes.
# ONE blank line, not two: one is how a paragraph is split to duck the cap, two is
# the separation PEP 8 already puts between a banner and the section under it.
_COMMENT_RUN_LINES = 8
_RUN_BLANK_BRIDGE = 1

# Docstring sections that are STRUCTURE, not prose: a parameter list is interface
# documentation and a call example is code, so neither counts against an essay budget
# aimed at narrative. Kept in step with _TRAILING_SECTIONS, which already accepts an
# Example: block after a pointer -- a shape the budget must not then penalise.
_DOCSTRING_SECTIONS = (
    "Args:",
    "Arguments:",
    "Returns:",
    "Yields:",
    "Raises:",
    "Attributes:",
    "Example:",
    "Examples:",
)

_ROOTS: tuple[Path, ...] = (Path("src/coder_eval"), Path("tests"))

# Exempt by (repo-relative path, function name) pair, and only for a function at module
# level: `Sandbox.run_command` is a method and a bare-name exemption would silently
# excuse it. Registered in src/coder_eval/cli/__init__.py.
_TYPER_COMMANDS = frozenset(
    {
        ("src/coder_eval/cli/run_command.py", "run_command"),
        ("src/coder_eval/cli/execute_command.py", "execute_command"),
        ("src/coder_eval/cli/plan_command.py", "plan_command"),
        ("src/coder_eval/cli/evaluate_command.py", "evaluate_command"),
        ("src/coder_eval/cli/report_command.py", "report_command"),
        ("src/coder_eval/cli/aggregate_command.py", "aggregate_command"),
        ("src/coder_eval/cli/export_command.py", "export_command"),
        ("src/coder_eval/cli/harbor_command.py", "reward_command"),
        ("src/coder_eval/cli/run_task_internal_command.py", "run_task_internal_command"),
    }
)

_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

_POINTER = re.compile(r"Rationale:\s*(\S+\.md)\s*§\s*(.+?)\s*$")

# The docstring delimiter, as a value: this module's own prose cannot spell it inline.
QUOTES = chr(34) * 3

# ``##`` or ``###``: a pointer may target a SUBSECTION, so appending to an existing
# section (the single-home rule) does not force the pointer up to the parent heading.
_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$")

# Executable directives that live in comments and therefore never enter the AST.
_DIRECTIVE = re.compile(r"^#\s*(?:noqa\b|type:\s*ignore\b|pyright:|nosec\b|pragma:|fmt:)")


class FileProse(NamedTuple):
    """Per-file prose measurement. ``essays`` is ``(qualname, words)``, descending."""

    docstring_words: int
    comment_words: int
    comment_blocks: int
    essays: list[tuple[str, int]]

    @property
    def total(self) -> int:
        return self.docstring_words + self.comment_words


class Measurement(NamedTuple):
    """Everything one tree scan produced: measured files, plus the ones that would not parse."""

    files: dict[Path, FileProse]
    skipped: list[Path]


def docstring_words(text: str) -> int:
    return len(text.split())


def comment_words(comment: str) -> int:
    """Words in one comment token. The ``#`` itself is not a word."""
    return len(comment.lstrip().removeprefix("#").split())


def _comment_runs(source: str) -> list[list[tokenize.TokenInfo]]:
    """Comment tokens grouped into runs of consecutive lines."""
    runs: list[list[tokenize.TokenInfo]] = []
    previous_line = -2
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        if token.start[0] == previous_line + 1 and runs:
            runs[-1].append(token)
        else:
            runs.append([token])
        previous_line = token.start[0]
    return runs


def prose_words(text: str) -> int:
    """``docstring_words`` minus the contents of any ``Args:``/``Returns:``/``Raises:``
    block.

    The 150-word bar is about NARRATIVE. A function with eight documented parameters
    is not writing an essay, and counting its parameter list pushed exactly the
    docstrings that document their contract best over the line.
    """
    lines = textwrap.dedent(text).split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() in _DOCSTRING_SECTIONS:
            base = len(lines[index]) - len(lines[index].lstrip())
            index += 1
            while index < len(lines) and (
                not lines[index].strip() or (len(lines[index]) - len(lines[index].lstrip())) > base
            ):
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return docstring_words("\n".join(kept))


def _is_interface_contract(node: ast.AST) -> bool:
    """True for an ``@abstractmethod``: its docstring IS the contract implementers read.

    The plugin SPI is the case. Exempting it by KIND rather than by name keeps the
    rule principled — a new abstract method is covered, and a long docstring that is
    not an interface contract still fails.
    """
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
        getattr(decorator, "id", getattr(decorator, "attr", "")) == "abstractmethod"
        for decorator in node.decorator_list
    )


def measure_source(source: str, rel: str) -> FileProse | None:
    """Measure one module's essay prose. ``None`` when it does not parse."""
    try:
        tree = ast.parse(source)
        runs = _comment_runs(source)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return None

    top_level = {id(node) for node in tree.body}
    essays: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        text = ast.get_docstring(node)
        if text is None:
            continue
        name = "<module>" if isinstance(node, ast.Module) else node.name
        if (rel, name) in _TYPER_COMMANDS and id(node) in top_level:
            continue
        if _is_interface_contract(node):
            continue
        words = prose_words(text)
        if words > _DOCSTRING_ESSAY_WORDS:
            essays.append((name, words))
    essays.sort(key=lambda essay: (-essay[1], essay[0]))

    blocks = [run for run in runs if len(run) >= _COMMENT_BLOCK_LINES]
    return FileProse(
        docstring_words=sum(words for _, words in essays),
        comment_words=sum(comment_words(token.string) for run in blocks for token in run),
        comment_blocks=len(blocks),
        essays=essays,
    )


def _roots(repo_root: Path) -> tuple[Path, ...]:
    """``_ROOTS``, after checking each one exists.

    Raises:
        FileNotFoundError: a root is not a directory — a renamed root must fail the gate,
            never measure zero files.
    """
    for root in _ROOTS:
        if not (repo_root / root).is_dir():
            raise FileNotFoundError(f"prose budget root does not exist: {root}")
    return _ROOTS


def _python_files(repo_root: Path) -> list[tuple[Path, Path]]:
    """``(absolute, repo-relative)`` for every ``*.py`` under each root, sorted."""
    return sorted(
        (path, path.relative_to(repo_root)) for root in _roots(repo_root) for path in (repo_root / root).rglob("*.py")
    )


def measure(repo_root: Path) -> Measurement:
    """Scan the configured roots. Files that do not parse are skipped, never fatal."""
    files: dict[Path, FileProse] = {}
    skipped: list[Path] = []
    for path, rel in _python_files(repo_root):
        prose = measure_source(path.read_text(encoding="utf-8"), rel.as_posix())
        if prose is None:
            skipped.append(rel)
        elif prose.total:
            files[rel] = prose
    return Measurement(files=files, skipped=skipped)


def total_words(files: dict[Path, FileProse]) -> int:
    """The one summing seam the report and the gate both use."""
    return sum(prose.total for prose in files.values())


def _subsystem(rel: Path) -> str:
    root = next(root for root in _ROOTS if rel.is_relative_to(root))
    below = rel.relative_to(root)
    return f"{root.as_posix()}/{below.parts[0]}" if len(below.parts) > 1 else root.as_posix()


def render_report(measurement: Measurement) -> str:
    grouped: dict[str, list[tuple[Path, FileProse]]] = {}
    for rel, prose in measurement.files.items():
        grouped.setdefault(_subsystem(rel), []).append((rel, prose))

    lines = ["PROSE BUDGET  (docstrings over 150 words + comment runs of 3+ lines)", ""]
    for subsystem in sorted(grouped, key=lambda name: (-sum(p.total for _, p in grouped[name]), name)):
        rows = sorted(grouped[subsystem], key=lambda row: (-row[1].total, row[0].as_posix()))
        lines.append(subsystem)
        for rel, prose in rows:
            lines.append(
                f"  {rel.as_posix():<64}{prose.docstring_words:>7} doc{prose.comment_words:>7} cmt{prose.total:>8}"
            )
        lines.append(f"  {'subtotal':<64}{'':>7}    {'':>7}    {sum(p.total for _, p in rows):>8}")
        lines.append("")

    files = measurement.files
    lines.append(
        f"TOTAL {total_words(files)}"
        f"  = {sum(p.docstring_words for p in files.values())} docstring"
        f" + {sum(p.comment_words for p in files.values())} comment"
    )
    lines.append(
        f"      files with prose {len(files)}"
        f"   essays {sum(len(p.essays) for p in files.values())}"
        f"   blocks {sum(p.comment_blocks for p in files.values())}"
    )
    if measurement.skipped:
        lines.append(f"skipped: {', '.join(p.as_posix() for p in measurement.skipped)}")

    roster = sorted(
        ((rel, name, words) for rel, prose in files.items() for name, words in prose.essays),
        key=lambda row: (-row[2], row[0].as_posix(), row[1]),
    )
    lines += ["", f"ESSAYS ({len(roster)} docstrings over {_DOCSTRING_ESSAY_WORDS} words)"]
    lines += [f"  {f'{rel.as_posix()}::{name}':<84}{words:>6}" for rel, name, words in roster]
    return "\n".join(lines) + "\n"


def _prose_lines(source: str) -> list[str]:
    """Every docstring and comment line in a module, for pointer scanning."""
    out: list[str] = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, _DOCSTRING_OWNERS):
                out += (ast.get_docstring(node) or "").splitlines()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                out.append(token.string)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return out
    return out


def _headings(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {match.group(1) for line in path.read_text(encoding="utf-8").splitlines() if (match := _HEADING.match(line))}


def check_pointers(repo_root: Path) -> list[str]:
    """Every ``Rationale: <path> § <heading>`` must resolve. Returns the failures."""
    failures: list[str] = []
    headings: dict[Path, set[str]] = {}
    for path, rel in _python_files(repo_root):
        for line in _prose_lines(path.read_text(encoding="utf-8")):
            match = _POINTER.search(line.strip())
            if not match:
                continue
            target, heading = Path(match.group(1)), match.group(2)
            if target not in headings:
                headings[target] = _headings(repo_root / target)
            if not (repo_root / target).is_file():
                failures.append(f"{rel.as_posix()}: no such file {target.as_posix()}")
            elif heading not in headings[target]:
                failures.append(f"{rel.as_posix()}: {target.as_posix()} has no heading '## {heading}'")
    return failures


_TRAILING_SECTIONS = ("Args:", "Returns:", "Raises:", "Yields:", "Example:", "Examples:")


def check_pointer_placement(repo_root: Path) -> list[str]:
    """A ``Rationale:`` pointer must be the LAST prose line of its block.

    Not style. A block replacement that anchors on the wrong line leaves the tail of the
    replaced prose stranded AFTER the pointer, where the ``$``-anchored ``_POINTER``
    regex cannot see it -- so the pointer still resolves and the file still parses while
    carrying a severed half-sentence.

    A docstring may follow its pointer with an ``Args:``/``Returns:``/``Raises:`` block,
    which is the house shape; anything else is the defect. An orphaned docstring
    terminator is reported too: a rewrite that leaves the original one behind makes the
    file unparseable, which the measurement silently reports as zero words.
    """
    failures: list[str] = []
    for path, rel_path in _python_files(repo_root):
        rel = rel_path.as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.split("\n")

        for index in range(1, len(lines)):
            if lines[index].strip() == QUOTES and lines[index - 1].strip().endswith(QUOTES):
                failures.append(f"{rel}:{index + 1}: orphaned docstring terminator")

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, _DOCSTRING_OWNERS):
                continue
            docstring = ast.get_docstring(node, clean=False)
            if docstring is None:
                continue
            body = [line for line in docstring.split("\n") if line.strip()]
            pointers = [i for i, line in enumerate(body) if _POINTER.search(line.strip())]
            if pointers:
                tail = body[pointers[-1] + 1 :]
                if tail and not tail[0].strip().startswith(_TRAILING_SECTIONS):
                    name = getattr(node, "name", "<module>")
                    failures.append(f"{rel}::{name}: prose after the Rationale pointer: {tail[0].strip()!r}")

        index = 0
        while index < len(lines):
            if not lines[index].strip().startswith("#"):
                index += 1
                continue
            end = index
            while end < len(lines) and lines[end].strip().startswith("#"):
                end += 1
            run = lines[index:end]
            for offset, line in enumerate(run):
                if _POINTER.search(line.strip()) and offset != len(run) - 1:
                    failures.append(
                        f"{rel}:{index + offset + 1}: comment continues after the Rationale pointer: "
                        f"{run[offset + 1].strip()!r}"
                    )
            index = end
    return failures


def own_comment_runs(source: str) -> list[tuple[int, int]]:
    """``(first line, length)`` for every own-line comment run, in file order.

    A run is consecutive own-line comments, reading through up to
    ``_RUN_BLANK_BRIDGE`` blank lines so that splitting a paragraph on whitespace
    does not read as several short comments. Code between two comments always ends
    the run. A trailing ``# noqa`` never starts one: it is a directive, not
    commentary.
    """
    lines = source.split("\n")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return []
    own = sorted(
        token.start[0]
        for token in tokens
        if token.type == tokenize.COMMENT and lines[token.start[0] - 1].strip().startswith("#")
    )
    runs: list[list[int]] = []
    for line in own:
        bridged = runs and line - runs[-1][-1] <= _RUN_BLANK_BRIDGE + 1
        if bridged and all(not lines[between - 1].strip() for between in range(runs[-1][-1] + 1, line)):
            runs[-1].append(line)
        else:
            runs.append([line])
    return [(run[0], len(run)) for run in runs]


def check_comment_runs(repo_root: Path) -> list[str]:
    """No own-line comment run may exceed :data:`_COMMENT_RUN_LINES`.

    A cap on the BLOCK, with no per-file allowance: a file may carry any number of
    one-line notes, and none of them may grow into a paragraph. The essay bar in
    prose, one granularity down.
    """
    return [
        f"{rel.as_posix()}:{line}: comment run of {length} lines "
        f"(bar is {_COMMENT_RUN_LINES}). Move the narrative to .claude/notes/."
        for path, rel in _python_files(repo_root)
        for line, length in own_comment_runs(path.read_text(encoding="utf-8"))
        if length > _COMMENT_RUN_LINES
    ]


def check_essays(repo_root: Path) -> list[str]:
    """No docstring may exceed the prose bar unless it is an interface contract.

    There is no numeric allowance. ``@abstractmethod`` and the Typer commands are
    exempt by KIND; everything else that trips the bar is narrative with a home in
    ``.claude/notes/``.
    """
    return [
        f"{rel.as_posix()}::{name}: {words} prose words in a docstring "
        f"(bar is {_DOCSTRING_ESSAY_WORDS}). Move the narrative to .claude/notes/."
        for rel, prose in sorted(measure(repo_root).files.items())
        for name, words in prose.essays
    ]


def collect_failures(repo_root: Path) -> list[str]:
    """Every check's failures, each prefixed with the check that raised it."""
    return (
        [f"unresolved pointer: {failure}" for failure in check_pointers(repo_root)]
        + [f"misplaced pointer: {failure}" for failure in check_pointer_placement(repo_root)]
        + [f"comment run: {failure}" for failure in check_comment_runs(repo_root)]
        + [f"docstring essay: {failure}" for failure in check_essays(repo_root)]
    )


def code_shape(source: str) -> str:
    """``ast.dump`` of the module with every docstring filtered out of its body.

    Filtered, not replaced by ``Pass``: a docstring-only body must not compare equal to
    a ``pass``-bodied one. ``ast.unparse``/``compile`` reject the resulting empty body;
    ``ast.dump`` does not.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    return ast.dump(tree)


def directive_comments(source: str) -> Counter[str]:
    """Multiset of functional directive comments (``# noqa``, ``# nosec``, ...).

    These never enter the AST, so deleting one is a behaviour change ``code_shape`` is
    structurally blind to.
    """
    found: Counter[str] = Counter()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return found
    for token in tokens:
        if token.type == tokenize.COMMENT and _DIRECTIVE.match(token.string.strip()):
            found[token.string.strip()] += 1
    return found


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout


def assert_code_unchanged(repo_root: Path, ref: str) -> list[str]:
    """Report every file under the configured roots whose code — not prose — differs from ``ref``."""
    roots = [root.as_posix() for root in _roots(repo_root)]
    code, listing = _git(repo_root, "diff", "--name-only", ref, "--", *roots)
    if code != 0:
        return [f"git diff against {ref!r} failed"]
    code, untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "--", *roots)
    if code != 0:
        return ["git ls-files for untracked files failed"]

    findings: list[str] = []
    for name in sorted(set(filter(None, (listing + untracked).splitlines()))):
        if not name.endswith(".py"):
            continue
        shown, before = _git(repo_root, "show", f"{ref}:{name}")
        before = before if shown == 0 else ""
        path = repo_root / name
        after = path.read_text(encoding="utf-8") if path.is_file() else ""
        try:
            if code_shape(before) != code_shape(after):
                findings.append(f"{name}: code changed (AST differs after stripping docstrings)")
        except SyntaxError:
            findings.append(f"{name}: could not parse both revisions")
            continue
        before_directives, after_directives = directive_comments(before), directive_comments(after)
        for verb, changed in (
            ("dropped", before_directives - after_directives),
            ("added", after_directives - before_directives),
        ):
            findings += [
                f"{name}: {verb} directive comment {comment!r} x{count}" for comment, count in sorted(changed.items())
            ]
    return findings


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parents[2]

    if argv[:1] == ["--assert-code-unchanged"]:
        if len(argv) != 2:
            print("usage: --assert-code-unchanged <git-ref>", file=sys.stderr)
            return 2
        findings = assert_code_unchanged(repo_root, argv[1])
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1 if findings else 0
    if argv:
        print("usage: prose_budget.py [--assert-code-unchanged <git-ref>]", file=sys.stderr)
        return 2

    print(render_report(measure(repo_root)), end="")

    failures = collect_failures(repo_root)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
