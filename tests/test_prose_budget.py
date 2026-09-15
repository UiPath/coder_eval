"""Unit tests for the prose budget measurement (``tests/lint/prose_budget.py``).

Every case runs against a synthetic tree in ``tmp_path`` or a plain string, never
against the real ``src/coder_eval`` — its word counts change with every prose commit,
so asserting on them here would make this file a second, drifting baseline.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.lint import prose_budget


def _words(count: int) -> str:
    return " ".join(f"w{index}" for index in range(count))


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / "src" / "coder_eval" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


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
        prose = prose_budget.measure_source(source, "cli/run_command.py")
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
        prose = prose_budget.measure_source(source, "cli/run_command.py")
        assert prose is not None
        assert prose.docstring_words == 400


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
        assert measurement.skipped == [Path("broken.py")]
        assert measurement.files[Path("good.py")].docstring_words == 200

    def test_total_words_sums_docstrings_and_comments(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {
                "a.py": f'def f():\n    """{_words(200)}"""\n',
                "b.py": "# one two\n# three four\n# five six\nx = 1\n",
            },
        )
        assert prose_budget.total_words(prose_budget.measure(root).files) == 206


class TestCheck:
    def test_at_the_baseline_it_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _tree(tmp_path, {"a.py": f'def f():\n    """{_words(200)}"""\n'})
        monkeypatch.setattr(prose_budget, "_ESSAY_BASELINE_WORDS", 200)
        assert prose_budget.check(root) is None

    def test_one_word_above_the_baseline_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _tree(tmp_path, {"a.py": f'def f():\n    """{_words(200)}"""\n'})
        monkeypatch.setattr(prose_budget, "_ESSAY_BASELINE_WORDS", 199)
        message = prose_budget.check(root)
        assert message is not None
        assert "200" in message and "199" in message


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
        assert "top-level" in report and "agents" in report
        assert "subtotal" in report
        assert "TOTAL 206" in report
        assert "ESSAYS" in report
        assert "a.py::essay_fn" in report
        assert "200" in report
