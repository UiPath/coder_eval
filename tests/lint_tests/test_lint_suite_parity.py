"""The witness that splitting the lint monolith moved tests without changing any.

`tests/test_custom_lint.py` was 9,680 lines across 52 top-level classes, one of which was 3,226 —
a third of the file. Splitting it is the kind of change that is either a pure move or a silent loss,
and the loss has two shapes this file pins:

* a test **dropped** — a class or method that landed in no module. Counted by NAME rather than by
  file, because the point of the split is that names moved.
* a whole module **failing to import**, which pytest reports as a collection error but which a
  "did the suite go green?" glance can miss when the remaining modules pass. A module that collects
  nothing is indistinguishable from one whose tests all pass.

Two things it deliberately does NOT assert. It does not pin the CLASS names: `TestPluginArtifacts`
became five classes on purpose, so a class-set equality check would have to be updated by the very
change it is meant to police. And it does not pin the count as a literal — the count is DERIVED from
the modules, so adding a test is not a chore here; what is pinned is that every test lives in
exactly one place and that every module contributes.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import tests.lint_tests


# Part of `make lint`: this file is the harness's own integrity check, so it must run wherever the
# rules it guards run. Applied at module scope — every assertion here is about the suite, not one rule.
pytestmark = pytest.mark.lint

SUITE = Path(__file__).parent
RUNNER = SUITE.parent / "test_custom_lint.py"

# The count at the split, recorded so a LOSS is visible. Deliberately a floor, not an equality: a
# new lint test must not have to edit this file, but 518 tests quietly becoming 400 must fail.
TESTS_AT_THE_SPLIT = 518


def _test_names(path: Path) -> list[str]:
    """Every `test_*` function in a module, QUALIFIED by its class.

    Qualified because two different rule classes legitimately carry the same method name —
    `test_ignores_files_outside_models` is the shape a dozen `BaseRule` tests share — so a bare
    name would report every one of those as a duplicate. What must be unique is the pair, which is
    also exactly what a class copied rather than moved would duplicate.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        names += [
            f"{cls.name}.{m.name}" for m in cls.body if isinstance(m, ast.FunctionDef) and m.name.startswith("test_")
        ]
    return names


def _suite_modules() -> list[Path]:
    return sorted(p for p in SUITE.glob("test_lint_*.py") if p.name != Path(__file__).name)


def test_the_suite_is_where_this_file_expects_it() -> None:
    """Anti-vacuity first: every assertion below is over a discovered set."""
    modules = _suite_modules()
    assert len(modules) >= 12, f"only {len(modules)} lint-test modules found — has the suite moved?"
    assert RUNNER.is_file(), "the runner-level invariants file is gone"


def test_no_test_name_is_claimed_by_two_modules() -> None:
    """A class copied into two modules rather than moved runs twice and is caught here.

    `pytest` would not complain — both copies pass — and the collected count would go UP, which
    reads like more coverage rather than a duplicated file.
    """
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for path in [*_suite_modules(), RUNNER]:
        for name in _test_names(path):
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} and {path.name}")
            seen[name] = path.name
    assert not duplicates, "these test names are defined twice:\n  " + "\n  ".join(duplicates)


def test_the_suite_still_holds_every_test_the_monolith_did() -> None:
    total = sum(len(_test_names(p)) for p in [*_suite_modules(), RUNNER])
    assert total >= TESTS_AT_THE_SPLIT, (
        f"the lint suite holds {total} tests, down from {TESTS_AT_THE_SPLIT} at the split. A move "
        "that loses a test is silent: the remaining modules still pass."
    )


@pytest.mark.parametrize("module", [p.stem for p in _suite_modules()], ids=lambda s: s.removeprefix("test_lint_"))
def test_every_module_imports_and_holds_tests(module: str) -> None:
    """A module that fails to import silently collects ZERO tests.

    Imported explicitly rather than trusted to `pytest`, because a collection error in one module
    does not stop the others from reporting green — and the tests it was holding then simply do not
    exist, with nothing naming them.
    """
    imported = importlib.import_module(f"tests.lint_tests.{module}")
    assert imported.__doc__, f"{module} has no docstring saying what it groups"
    assert _test_names(SUITE / f"{module}.py"), f"{module} holds no tests"


def test_the_package_exposes_every_module_it_ships() -> None:
    """`pkgutil` against the directory listing: a module nobody imports is a module nobody runs."""
    discovered = {m.name for m in pkgutil.iter_modules(tests.lint_tests.__path__)}
    on_disk = {p.stem for p in SUITE.glob("*.py") if p.stem != "__init__"}
    assert discovered == on_disk, f"discovery and the directory disagree: {discovered ^ on_disk}"


def test_every_test_class_carries_the_lint_marker() -> None:
    """`make lint` selects on the MARKER, so an unmarked class is a rule nobody runs there.

    It used to select by PATH — `pytest tests/test_custom_lint.py` — which ran every test in that
    file whether marked or not, so the marker and the target disagreed about what a lint test is and
    nothing noticed. The split exposed it in both directions: five classes (CE047, CE050, CE051 and
    the two import-resolution helpers) had never been marked and would have dropped out, while
    `tests/test_lint_no_top_level_run_limits.py` was marked and had never been IN, because it was
    not the one file the target named.

    So the marker is now the single definition of "runs under `make lint`", and this is what keeps it
    one: a new module or class here is covered the moment it is marked, wherever the file sits.
    """
    unmarked: list[str] = []
    for path in [*_suite_modules(), SUITE / "plugin_base.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in n.targets)
            for n in tree.body
        ):
            continue
        unmarked += [
            f"{path.name}::{n.name}"
            for n in tree.body
            if isinstance(n, ast.ClassDef)
            and n.name.startswith("Test")
            and not any("lint" in ast.unparse(d) for d in n.decorator_list)
        ]
    assert not unmarked, (
        "these test classes carry no `@pytest.mark.lint`, so `make lint` does not run them:\n  " + "\n  ".join(unmarked)
    )
