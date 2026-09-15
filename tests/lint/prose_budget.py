"""Measure and ratchet the essay-shaped prose in ``src/coder_eval``.

One gated number: ``essay_words`` — words in docstrings over 150 words (Typer command
docstrings exempt, they render as ``--help``) plus words in comment runs of three or
more consecutive lines. It may not exceed ``_ESSAY_BASELINE_WORDS``, so the house style
is *no new essays*, not *no new documentation*.

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
import tokenize
from collections import Counter
from pathlib import Path
from typing import NamedTuple


_DOCSTRING_ESSAY_WORDS = 150
_COMMENT_BLOCK_LINES = 3
_ESSAY_BASELINE_WORDS = 73_413

_SRC = Path("src/coder_eval")

# Exempt by (path relative to src/coder_eval, function name) pair, and only for a
# function at module level: `Sandbox.run_command` is a method and a bare-name exemption
# would silently excuse it. Registered in src/coder_eval/cli/__init__.py.
_TYPER_COMMANDS = frozenset(
    {
        ("cli/run_command.py", "run_command"),
        ("cli/execute_command.py", "execute_command"),
        ("cli/plan_command.py", "plan_command"),
        ("cli/evaluate_command.py", "evaluate_command"),
        ("cli/report_command.py", "report_command"),
        ("cli/aggregate_command.py", "aggregate_command"),
        ("cli/export_command.py", "export_command"),
        ("cli/harbor_command.py", "reward_command"),
        ("cli/run_task_internal_command.py", "run_task_internal_command"),
    }
)

_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

_POINTER = re.compile(r"Rationale:\s*(\S+\.md)\s*§\s*(.+?)\s*$")

_HEADING = re.compile(r"^##\s+(.+?)\s*$")

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
        words = docstring_words(text)
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


def measure(repo_root: Path) -> Measurement:
    """Scan ``src/coder_eval``. Files that do not parse are skipped, never fatal."""
    files: dict[Path, FileProse] = {}
    skipped: list[Path] = []
    for path in sorted((repo_root / _SRC).rglob("*.py")):
        rel = path.relative_to(repo_root / _SRC)
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
    return rel.parts[0] if len(rel.parts) > 1 else "top-level"


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
                f"  {rel.as_posix():<48}{prose.docstring_words:>7} doc{prose.comment_words:>7} cmt{prose.total:>8}"
            )
        lines.append(f"  {'subtotal':<48}{'':>7}    {'':>7}    {sum(p.total for _, p in rows):>8}")
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
    lines += [f"  {f'{rel.as_posix()}::{name}':<68}{words:>6}" for rel, name, words in roster]
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
    for path in sorted((repo_root / _SRC).rglob("*.py")):
        rel = path.relative_to(repo_root / _SRC)
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


def check(repo_root: Path) -> str | None:
    """``None`` when the tree is at or under the baseline, else the failure message."""
    total = total_words(measure(repo_root).files)
    if total <= _ESSAY_BASELINE_WORDS:
        return None
    return (
        f"prose budget exceeded: {total} essay words against a baseline of "
        f"{_ESSAY_BASELINE_WORDS} (+{total - _ESSAY_BASELINE_WORDS}). "
        "Move rationale to .claude/notes/, or lower the baseline if you removed prose."
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
    """Report every ``src/coder_eval`` file whose code — not prose — differs from ``ref``."""
    code, listing = _git(repo_root, "diff", "--name-only", ref, "--", _SRC.as_posix())
    if code != 0:
        return [f"git diff against {ref!r} failed"]

    findings: list[str] = []
    for name in sorted(filter(None, listing.splitlines())):
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
        dropped = directive_comments(before) - directive_comments(after)
        findings += [
            f"{name}: dropped directive comment {comment!r} x{count}" for comment, count in sorted(dropped.items())
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

    print(render_report(measure(repo_root)), end="")

    failed = False
    for failure in check_pointers(repo_root):
        print(f"unresolved pointer: {failure}", file=sys.stderr)
        failed = True
    if (message := check(repo_root)) is not None:
        print(message, file=sys.stderr)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
