"""Unit tests for the prose budget measurement (``tests/lint/prose_budget.py``).

Every case runs against a synthetic tree in ``tmp_path`` or a plain string, never
against the real tree: its word counts change with every prose commit, so a test
that asserted on them would fail on unrelated edits.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.lint import prose_budget


def _words(count: int) -> str:
    return " ".join(f"w{index}" for index in range(count))


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "tests").mkdir(exist_ok=True)
    return _write(tmp_path, {f"src/coder_eval/{rel}": text for rel, text in files.items()})


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


class TestDocstringMeasurement:
    def test_long_function_docstring_is_counted(self) -> None:
        prose = prose_budget.measure_source(f'def f():\n    """{_words(200)}"""\n', "m.py")
        assert prose is not None
        assert prose.docstring_words == 200
        assert prose.essays == [("f", 200)]

    def test_exactly_the_threshold_is_not_an_essay(self) -> None:
        prose = prose_budget.measure_source(f'def f():\n    """{_words(150)}"""\n', "m.py")
        assert prose is not None
        assert prose.docstring_words == 0
        assert prose.essays == []

    def test_one_word_over_the_threshold_is_counted(self) -> None:
        prose = prose_budget.measure_source(f'def f():\n    """{_words(151)}"""\n', "m.py")
        assert prose is not None
        assert prose.docstring_words == 151

    def test_module_docstring_is_named_module(self) -> None:
        prose = prose_budget.measure_source(f'"""{_words(200)}"""\n', "m.py")
        assert prose is not None
        assert prose.essays == [("<module>", 200)]


class TestTyperExemption:
    def test_module_level_typer_command_is_exempt(self) -> None:
        source = f'def run_command():\n    """{_words(400)}"""\n'
        prose = prose_budget.measure_source(source, "src/coder_eval/cli/run_command.py")
        assert prose is not None
        assert prose.docstring_words == 0

    def test_same_name_as_a_method_elsewhere_is_counted(self) -> None:
        """``Sandbox.run_command`` — the case a bare-name exemption would silently excuse."""
        source = f'class Sandbox:\n    def run_command(self):\n        """{_words(400)}"""\n'
        prose = prose_budget.measure_source(source, "sandbox.py")
        assert prose is not None
        assert prose.docstring_words == 400

    def test_same_name_nested_inside_the_exempt_module_is_counted(self) -> None:
        source = f'class Helper:\n    def run_command(self):\n        """{_words(400)}"""\n'
        prose = prose_budget.measure_source(source, "src/coder_eval/cli/run_command.py")
        assert prose is not None
        assert prose.docstring_words == 400

    def test_every_exempt_pair_still_exists(self) -> None:
        """An allowlist entry whose function has moved or been renamed exempts nothing
        while still reading as a deliberate exemption -- the same vacuous-guarantee
        failure CE057's membership test exists to catch."""
        import ast

        repo_root = Path(__file__).resolve().parents[1]
        for rel, name in sorted(prose_budget._TYPER_COMMANDS):
            path = repo_root / rel
            assert path.is_file(), f"_TYPER_COMMANDS names {rel}, which does not exist"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level = {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
            assert name in top_level, f"_TYPER_COMMANDS names {rel}::{name}, which is not defined there"


class TestCommentMeasurement:
    def test_three_consecutive_lines_are_a_block(self) -> None:
        prose = prose_budget.measure_source("# one two\n# three four\n# five six\nx = 1\n", "m.py")
        assert prose is not None
        assert prose.comment_words == 6
        assert prose.comment_blocks == 1

    def test_a_bare_hash_adds_no_words_but_keeps_the_run_length(self) -> None:
        prose = prose_budget.measure_source("# one two\n#\n# five six\nx = 1\n", "m.py")
        assert prose is not None
        assert prose.comment_words == 4
        assert prose.comment_blocks == 1

    def test_two_consecutive_lines_are_not_a_block(self) -> None:
        prose = prose_budget.measure_source("# one two\n# three four\nx = 1\n", "m.py")
        assert prose is not None
        assert prose.comment_words == 0

    def test_a_blank_line_splits_the_run(self) -> None:
        prose = prose_budget.measure_source("# one two\n# three four\n\n# five six\nx = 1\n", "m.py")
        assert prose is not None
        assert prose.comment_words == 0

    def test_two_separate_runs_are_both_counted(self) -> None:
        source = "# a b\n# c d\n# e f\nx = 1\n# g h\n# i j\n# k l\n# m n\ny = 2\n"
        prose = prose_budget.measure_source(source, "m.py")
        assert prose is not None
        assert prose.comment_words == 14
        assert prose.comment_blocks == 2

    def test_the_hash_is_not_a_word(self) -> None:
        assert prose_budget.comment_words("# foo bar") == 2


class TestMeasureTree:
    def test_a_syntax_error_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "broken.py": "def f(:\n",
                "good.py": f'def f():\n    """{_words(200)}"""\n',
            },
        )
        measurement = prose_budget.measure(root)
        assert measurement.skipped == [Path("src/coder_eval/broken.py")]
        assert measurement.files[Path("src/coder_eval/good.py")].docstring_words == 200

    def test_total_words_sums_docstrings_and_comments(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "a.py": f'def f():\n    """{_words(200)}"""\n',
                "b.py": "# one two\n# three four\n# five six\nx = 1\n",
            },
        )
        assert prose_budget.total_words(prose_budget.measure(root).files) == 206


class TestCommentBudget:
    """The per-file comment budget: a floor, then a share of the file's length."""

    def test_the_floor_applies_to_a_small_file(self) -> None:
        assert prose_budget.comment_line_budget(10) == 20
        assert prose_budget.comment_line_budget(100) == 20

    def test_the_ratio_applies_once_it_beats_the_floor(self) -> None:
        assert prose_budget.comment_line_budget(1000) == 150

    def test_a_file_within_its_budget_passes(self, tmp_path: Path) -> None:
        body = "\n".join(["x = 1"] * 200)
        root = _tree(tmp_path, {"a.py": "# one\n# two\n" + body})
        assert prose_budget.check_comment_density(root) == []

    def test_a_file_over_its_budget_fails(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"a.py": "\n".join(["# pad"] * 40) + "\nx = 1\n"})
        failures = prose_budget.check_comment_density(root)
        assert len(failures) == 1
        assert "own-line comments" in failures[0]

    def test_a_trailing_comment_is_a_directive_not_commentary(self, tmp_path: Path) -> None:
        """40 trailing `# noqa` must not consume the budget; 40 own-line ones would."""
        root = _tree(tmp_path, {"a.py": "\n".join(["x = 1  # noqa: E501"] * 40) + "\n"})
        assert prose_budget.check_comment_density(root) == []

    def test_the_budget_shrinks_with_the_file(self) -> None:
        """The point of a ratio: deleting code takes its comment budget with it."""
        assert prose_budget.comment_line_budget(2000) > prose_budget.comment_line_budget(1000)


class TestProseWords:
    def test_an_args_block_does_not_count(self) -> None:
        doc = f"Summary.\n\n{_words(140)}\n\nArgs:\n    a: {_words(100)}\n"
        assert prose_budget.prose_words(doc) < 150
        assert prose_budget.docstring_words(doc) > 150

    def test_prose_after_a_section_still_counts(self) -> None:
        """A trailing contract paragraph is prose, not structure."""
        doc = f"Summary.\n\nArgs:\n    a: thing\n\n{_words(200)}\n"
        assert prose_budget.prose_words(doc) > 150

    def test_returns_and_raises_are_structure_too(self) -> None:
        doc = f"Summary.\n\nReturns:\n    {_words(90)}\n\nRaises:\n    ValueError: {_words(90)}\n"
        assert prose_budget.prose_words(doc) < 150

    def test_an_example_block_is_structure_not_prose(self) -> None:
        """A call example is code. Counting it penalised exactly the docstrings that
        show a caller how to use the thing -- and ``_TRAILING_SECTIONS`` already
        accepts an ``Example:`` block after a pointer, so the two must agree."""
        doc = f"Summary.\n\nExample:\n    >>> f({_words(200)})\n"
        assert prose_budget.prose_words(doc) < 150
        assert prose_budget.docstring_words(doc) > 150

    def test_the_two_section_lists_agree_on_example(self) -> None:
        for section in ("Example:", "Examples:"):
            assert section in prose_budget._DOCSTRING_SECTIONS
            assert section in prose_budget._TRAILING_SECTIONS


class TestInterfaceContractExemption:
    def test_an_abstractmethod_docstring_is_exempt(self) -> None:
        source = (
            "from abc import ABC, abstractmethod\n\n"
            "class A(ABC):\n"
            "    @abstractmethod\n"
            f"    def f(self):\n        {Q}{_words(400)}{Q}\n"
        )
        prose = prose_budget.measure_source(source, "m.py")
        assert prose is not None
        assert prose.essays == []

    def test_a_plain_method_is_not_exempt(self) -> None:
        source = f"class A:\n    def f(self):\n        {Q}{_words(400)}{Q}\n"
        prose = prose_budget.measure_source(source, "m.py")
        assert prose is not None
        assert prose.essays == [("f", 400)]


class TestPointers:
    def _root(self, tmp_path: Path, source: str, notes: str) -> Path:
        root = _tree(tmp_path, {"a.py": source})
        target = root / ".claude" / "notes" / "timing.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(notes, encoding="utf-8")
        return root

    def test_a_resolving_pointer_passes(self, tmp_path: Path) -> None:
        source = 'def f():\n    """Do it.\n\n    Rationale: .claude/notes/timing.md § close_window\n    """\n'
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n\nWhy.\n")
        assert prose_budget.check_pointers(root) == []

    def test_a_pointer_in_a_comment_resolves(self, tmp_path: Path) -> None:
        source = "# Rationale: .claude/notes/timing.md § close_window\nx = 1\n"
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n\nWhy.\n")
        assert prose_budget.check_pointers(root) == []

    def test_a_missing_file_fails(self, tmp_path: Path) -> None:
        source = '"""Rationale: .claude/notes/absent.md § close_window"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n")
        failures = prose_budget.check_pointers(root)
        assert len(failures) == 1
        assert "no such file" in failures[0]

    def test_a_missing_heading_fails(self, tmp_path: Path) -> None:
        source = '"""Rationale: .claude/notes/timing.md § open_window"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n")
        failures = prose_budget.check_pointers(root)
        assert len(failures) == 1
        assert "no heading" in failures[0]

    def test_a_heading_with_backticks_resolves(self, tmp_path: Path) -> None:
        source = '"""Rationale: .claude/notes/timing.md § `--resume` is command-relative"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## `--resume` is command-relative\n")
        assert prose_budget.check_pointers(root) == []

    def test_a_subsection_heading_resolves(self, tmp_path: Path) -> None:
        """A pointer may target a ``###``: the single-home rule appends to a section."""
        source = '"""Rationale: .claude/notes/timing.md § The clamp"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n\n### The clamp\n\nWhy.\n")
        assert prose_budget.check_pointers(root) == []

    def test_a_level_one_heading_does_not_resolve(self, tmp_path: Path) -> None:
        source = '"""Rationale: .claude/notes/timing.md § Timing"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## close_window\n")
        assert len(prose_budget.check_pointers(root)) == 1

    def test_a_heading_with_a_colon_resolves(self, tmp_path: Path) -> None:
        source = '"""Rationale: .claude/notes/timing.md § Execute vs. run: the grading switch"""\n'
        root = self._root(tmp_path, source, "# Timing\n\n## Execute vs. run: the grading switch\n")
        assert prose_budget.check_pointers(root) == []


class TestCodeShape:
    def test_prose_only_differences_compare_equal(self) -> None:
        before = 'def f(a):\n    """One.\n\n    Long rationale.\n    """\n    # why\n    # more why\n    return a + 1\n'
        after = 'def f(a):\n    """Two."""\n    return a + 1\n'
        assert prose_budget.code_shape(before) == prose_budget.code_shape(after)

    def test_a_one_statement_edit_differs(self) -> None:
        before = 'def f(a):\n    """One."""\n    return a + 1\n'
        after = 'def f(a):\n    """One."""\n    return a + 2\n'
        assert prose_budget.code_shape(before) != prose_budget.code_shape(after)

    def test_a_docstring_only_body_does_not_raise(self) -> None:
        assert prose_budget.code_shape('def f():\n    """Only this."""\n')

    def test_a_docstring_only_body_is_not_a_pass_body(self) -> None:
        docstring_only = prose_budget.code_shape('def f():\n    """Only this."""\n')
        pass_body = prose_budget.code_shape("def f():\n    pass\n")
        assert docstring_only != pass_body


class TestDirectiveComments:
    def test_a_directive_inside_a_block_is_found(self) -> None:
        source = "# context\n# more context\nx = 1  # noqa: CE051\n"
        assert prose_budget.directive_comments(source) == {"# noqa: CE051": 1}

    def test_dropping_a_directive_shows_up_as_a_difference(self) -> None:
        before = "# context\n# noqa: CE051\n# more\nx = 1\n"
        after = "# context\n# more\nx = 1\n"
        dropped = prose_budget.directive_comments(before) - prose_budget.directive_comments(after)
        assert dropped == {"# noqa: CE051": 1}

    def test_ordinary_prose_is_not_a_directive(self) -> None:
        assert prose_budget.directive_comments("# just a comment\nx = 1\n") == {}

    @pytest.mark.parametrize(
        "comment",
        [
            "# type: ignore[return-value]",
            "# pyright: ignore[reportMissingImports]",
            "# pyright: reportIncompatibleVariableOverride=false",
            "# nosec B310",
            "# pragma: no cover",
            "# fmt: off",
        ],
    )
    def test_every_tracked_directive_form_is_recognised(self, comment: str) -> None:
        assert prose_budget.directive_comments(f"x = 1  {comment}\n") == {comment: 1}


class TestRenderReport:
    def test_the_report_names_files_subsystems_the_total_and_the_essays(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "a.py": f'def essay_fn():\n    """{_words(200)}"""\n',
                "agents/b.py": "# one two\n# three four\n# five six\nx = 1\n",
            },
        )
        report = prose_budget.render_report(prose_budget.measure(root))
        assert "a.py" in report
        assert "src/coder_eval/agents\n" in report
        assert "src/coder_eval\n" in report
        assert "subtotal" in report
        assert "TOTAL 206" in report
        assert "ESSAYS" in report
        assert "a.py::essay_fn" in report
        assert "200" in report


# The docstring delimiter as a value, so a fixture can embed one without ending this file's
# own strings.
Q = chr(34) * 3


class TestPointerPlacement:
    """A ``Rationale:`` pointer must be the last prose line of its block."""

    def _root(self, tmp_path: Path, source: str) -> Path:
        root = _tree(tmp_path, {"a.py": source})
        target = root / ".claude" / "notes" / "timing.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Timing\n\n## close_window\n\nWhy.\n", encoding="utf-8")
        return root

    def test_a_pointer_at_the_end_of_a_comment_run_passes(self, tmp_path: Path) -> None:
        source = "# One.\n# Two.\n# Rationale: .claude/notes/timing.md \u00a7 close_window\nx = 1\n"
        assert prose_budget.check_pointer_placement(self._root(tmp_path, source)) == []

    def test_a_comment_run_that_continues_after_its_pointer_fails(self, tmp_path: Path) -> None:
        source = "# One.\n# Rationale: .claude/notes/timing.md \u00a7 close_window\n# severed tail.\nx = 1\n"
        failures = prose_budget.check_pointer_placement(self._root(tmp_path, source))
        assert len(failures) == 1
        assert "severed tail" in failures[0]

    def test_a_docstring_pointer_followed_by_args_passes(self, tmp_path: Path) -> None:
        source = (
            "def f(a):\n"
            f"    {Q}Do it.\n\n"
            "    Rationale: .claude/notes/timing.md \u00a7 close_window\n\n"
            "    Args:\n"
            "        a: thing\n"
            f"    {Q}\n"
        )
        assert prose_budget.check_pointer_placement(self._root(tmp_path, source)) == []

    def test_a_docstring_with_prose_after_its_pointer_fails(self, tmp_path: Path) -> None:
        source = (
            "def f():\n"
            f"    {Q}Do it.\n\n"
            "    Rationale: .claude/notes/timing.md \u00a7 close_window\n"
            "    and the stranded half of a sentence.\n"
            f"    {Q}\n"
        )
        failures = prose_budget.check_pointer_placement(self._root(tmp_path, source))
        assert len(failures) == 1
        assert "stranded half" in failures[0]

    def test_an_orphaned_docstring_terminator_is_reported(self, tmp_path: Path) -> None:
        source = f"def f():\n    {Q}Do it.{Q}\n    {Q}\n    return 1\n"
        failures = prose_budget.check_pointer_placement(self._root(tmp_path, source))
        assert any("orphaned docstring terminator" in failure for failure in failures)

    def test_a_file_with_no_pointer_is_not_flagged(self, tmp_path: Path) -> None:
        source = "# One.\n# Two.\n# Three.\nx = 1\n"
        assert prose_budget.check_pointer_placement(self._root(tmp_path, source)) == []

    def test_a_syntax_error_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        assert prose_budget.check_pointer_placement(self._root(tmp_path, "def f(:\n")) == []


_ESSAY = f'def f():\n    """{_words(200)}"""\n'
_DENSE = "\n".join(["# pad"] * 40) + "\nx = 1\n"
_UNRESOLVED = '"""Rationale: .claude/notes/absent.md § nowhere"""\n'


class TestRoots:
    def test_a_missing_root_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("nope"),))
        with pytest.raises(FileNotFoundError, match="nope"):
            prose_budget.measure(tmp_path)

    def test_two_roots_are_both_measured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _write(tmp_path, {"src/coder_eval/a.py": _ESSAY, "tests/b.py": _ESSAY})
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("src/coder_eval"), Path("tests")))
        assert set(prose_budget.measure(root).files) == {Path("src/coder_eval/a.py"), Path("tests/b.py")}

    def test_default_roots_include_tests(self, tmp_path: Path) -> None:
        root = _write(tmp_path, {"src/coder_eval/a.py": _ESSAY, "tests/b.py": _ESSAY})
        assert set(prose_budget.measure(root).files) == {Path("src/coder_eval/a.py"), Path("tests/b.py")}

    def test_density_and_essays_follow_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _write(tmp_path, {"src/coder_eval/a.py": "x = 1\n", "tests/c.py": _DENSE, "tests/e.py": _ESSAY})
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("src/coder_eval"),))
        assert prose_budget.check_comment_density(root) == []
        assert prose_budget.check_essays(root) == []
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("tests"),))
        density = prose_budget.check_comment_density(root)
        assert len(density) == 1
        assert density[0].startswith("tests/c.py: ")
        essays = prose_budget.check_essays(root)
        assert len(essays) == 1
        assert essays[0].startswith("tests/e.py::f: ")

    def test_pointer_checks_follow_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        severed = "# One.\n# Rationale: .claude/notes/timing.md § close_window\n# severed tail.\nx = 1\n"
        root = _write(
            tmp_path,
            {
                "src/coder_eval/a.py": "x = 1\n",
                "tests/d.py": _UNRESOLVED,
                "tests/s.py": severed,
                ".claude/notes/timing.md": "# Timing\n\n## close_window\n",
            },
        )
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("src/coder_eval"),))
        assert prose_budget.check_pointers(root) == []
        assert prose_budget.check_pointer_placement(root) == []
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("tests"),))
        pointers = prose_budget.check_pointers(root)
        assert len(pointers) == 1
        assert pointers[0].startswith("tests/d.py: ")
        placement = prose_budget.check_pointer_placement(root)
        assert len(placement) == 1
        assert placement[0].startswith("tests/s.py:")

    def test_subsystem_names_the_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _write(tmp_path, {"tests/x.py": _ESSAY, "tests/lint/y.py": _ESSAY})
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("tests"),))
        report = prose_budget.render_report(prose_budget.measure(root))
        assert "\ntests/lint\n" in report
        assert "\ntests\n" in report


class TestCollectFailures:
    def test_every_check_is_prefixed(self, tmp_path: Path) -> None:
        misplaced = f"def g():\n    {Q}Do it.\n\n    Rationale: .claude/notes/absent.md § x\n    tail.\n    {Q}\n"
        root = _tree(tmp_path, {"essay.py": _ESSAY, "dense.py": _DENSE, "misplaced.py": misplaced})
        prefixes = {failure.split(": ", 1)[0] for failure in prose_budget.collect_failures(root)}
        assert prefixes == {"unresolved pointer", "misplaced pointer", "comment budget", "docstring essay"}

    def test_a_clean_tree_has_no_failures(self, tmp_path: Path) -> None:
        assert prose_budget.collect_failures(_tree(tmp_path, {"a.py": "x = 1\n"})) == []


class TestAssertCodeUnchanged:
    def _repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = _write(tmp_path, {"tests/x.py": 'def f():\n    """One."""\n    return 1\n'})
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "init")
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("tests"),))
        return root

    def test_a_test_file_code_change_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._repo(tmp_path, monkeypatch)
        _write(root, {"tests/x.py": 'def f():\n    """One."""\n    return 2\n'})
        findings = prose_budget.assert_code_unchanged(root, "HEAD")
        assert len(findings) == 1
        assert findings[0].startswith("tests/x.py: code changed")

    def test_an_added_directive_comment_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._repo(tmp_path, monkeypatch)
        _write(root, {"tests/x.py": 'def f():\n    """One."""\n    return 1  # noqa: E501\n'})
        findings = prose_budget.assert_code_unchanged(root, "HEAD")
        assert findings == ["tests/x.py: added directive comment '# noqa: E501' x1"]

    def test_a_new_untracked_file_with_code_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._repo(tmp_path, monkeypatch)
        _write(root, {"tests/new.py": "x = 1\n"})
        findings = prose_budget.assert_code_unchanged(root, "HEAD")
        assert findings == ["tests/new.py: code changed (AST differs after stripping docstrings)"]

    def test_a_missing_root_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._repo(tmp_path, monkeypatch)
        monkeypatch.setattr(prose_budget, "_ROOTS", (Path("nope"),))
        with pytest.raises(FileNotFoundError, match="nope"):
            prose_budget.assert_code_unchanged(root, "HEAD")

    def test_a_docstring_only_change_is_not_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._repo(tmp_path, monkeypatch)
        _write(root, {"tests/x.py": 'def f():\n    """Two, longer."""\n    return 1\n'})
        assert prose_budget.assert_code_unchanged(root, "HEAD") == []


class TestMain:
    def test_an_unknown_argument_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert prose_budget.main(["--assert-code-unchanged-typo", "HEAD"]) == 2
        assert "usage:" in capsys.readouterr().err
