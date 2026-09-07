"""Task-path expansion, which decides what a CI gate actually measures.

`expand_task_files` had no direct test: its three other references all patch it
out. The contract the docs and the `ci` skill publish is that a glob matching
nothing exits 1 -- so a stale entry in a multi-line `args:` block cannot leave a
gate green over tasks it never ran.
"""

from pathlib import Path

import pytest
import typer

from coder_eval.cli.run_helpers import expand_task_files


@pytest.fixture
def tasks_tree(tmp_path, monkeypatch):
    """A suite at two depths, so `**` recursion is exercised for real."""
    (tmp_path / "tasks" / "sub").mkdir(parents=True)
    (tmp_path / "tasks" / "top.yaml").write_text("task_id: top\n", encoding="utf-8")
    (tmp_path / "tasks" / "sub" / "deep.yaml").write_text("task_id: deep\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _names(paths):
    return sorted(p.name for p in paths)


class TestRecursiveGlob:
    # The published snippets all use `**`, and the lint rule that used to ban it
    # was removed in favour of this promise. A switch to `glob.glob`, which is
    # non-recursive by default, would silently drop the top-level task.
    def test_double_star_matches_both_depths(self, tasks_tree):
        assert _names(expand_task_files([Path("tasks/**/*.yaml")])) == [
            "deep.yaml",
            "top.yaml",
        ]

    def test_a_literal_file_is_passed_through(self, tasks_tree):

        assert _names(expand_task_files([Path("tasks/top.yaml")])) == ["top.yaml"]


class TestFailsClosed:
    def test_a_single_unmatched_pattern_exits(self, tasks_tree):

        with pytest.raises(typer.Exit):
            expand_task_files([Path("nope/*.yaml")])

    # The regression this guards: accumulating across patterns and checking only
    # the union meant one renamed suite among several ran the survivors and
    # exited 0, reporting green over unmeasured tasks.
    def test_one_stale_pattern_among_several_exits(self, tasks_tree):

        with pytest.raises(typer.Exit):
            expand_task_files([Path("tasks/*.yaml"), Path("renamed/*.yaml")])

    def test_the_unmatched_pattern_is_named(self, tasks_tree, capsys):

        with pytest.raises(typer.Exit):
            expand_task_files([Path("tasks/*.yaml"), Path("renamed/*.yaml")])
        out = capsys.readouterr().out
        assert "renamed" in out
        # The one that DID match is not reported as a failure.
        assert "no match: tasks/" not in out

    # A directory that exists but holds no task file is the same failure as a
    # typo: the caller named it and expected tasks there.
    def test_an_empty_directory_exits(self, tasks_tree):

        with pytest.raises(typer.Exit):
            expand_task_files([Path("empty/*.yaml")])

    def test_no_patterns_at_all_exits(self, tasks_tree):
        with pytest.raises(typer.Exit):
            expand_task_files([])
