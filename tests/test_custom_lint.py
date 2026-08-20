"""The RUNNER-level invariants of the custom lint harness.

Three assertions about the harness itself: that every wired `BaseRule` reports no violation against
`src/`, and that no rule id collides — once within `ALL_RULES`, and once across `ALL_RULES` and the
`@pytest.mark.lint` test classes, which `runner.py`'s own assert cannot see.

**The per-rule tests live in `tests/lint_tests/`, grouped by what each rule reasons over.** This
file was 9,680 lines and 52 classes; a third of it was one class. Splitting it changed no test —
`tests/lint_tests/test_lint_suite_parity.py` is the witness — and the groups are: `ast_rules` and
`ast_seams` (the `BaseRule` classes), `import_resolution`, `doc_surfaces`, `onboarding_surfaces`,
`task_surfaces`, `harness_meta`, `computed_claims`, and five `plugin_*` modules for the shipped
plugin's surfaces.

Run just these tests:
    uv run pytest -m lint -v      # what `make lint` runs: the marker, never a path
    make lint
"""

import ast
import re
from pathlib import Path

import pytest

from tests.lint.runner import ALL_RULES, check_paths
from tests.lint_tests.shared import SRC


@pytest.mark.lint
@pytest.mark.parametrize("rule_class", ALL_RULES, ids=[r.id for r in ALL_RULES])
def test_no_violations(rule_class: type) -> None:
    import sys

    mod_doc = (getattr(sys.modules.get(rule_class.__module__), "__doc__", "") or "").splitlines()[0].strip()
    violations = check_paths([SRC], rules=[rule_class])
    assert not violations, (
        f"\n{len(violations)} violation(s) for {rule_class.id} ({mod_doc}):\n\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\nFix the violation or add `# noqa: {rule_class.id}` to the offending line with a comment explaining why."
    )


@pytest.mark.lint
def test_rule_ids_unique() -> None:
    """No two lint rules may share a CE id (anti-shadow: a noqa keys on the id).

    Mirrors the AgentRegistry / register_pricing anti-shadow invariant. pytest
    silently auto-suffixes duplicate parametrize ids, so without this a duplicate
    CE number (e.g. two in-flight branches claiming the next number) would not
    fail the suite on its own.
    """
    ids = [r.id for r in ALL_RULES]
    assert len(set(ids)) == len(ids), f"duplicate CE rule id(s): {sorted({i for i in ids if ids.count(i) > 1})}"


@pytest.mark.lint
def test_rule_ids_are_unique_across_baserules_and_test_classes() -> None:
    """The uniqueness invariant, extended to the HALF `ALL_RULES` cannot see.

    Roughly a third of the CE rules are not `BaseRule`s at all — CE026-CE031, CE033-CE036,
    CE038-CE039, CE043 and CE045-CE046 are `@pytest.mark.lint` classes, because their subject is
    Markdown, YAML, a resolved Typer signature or the whole `src/` tree rather than one `.py` AST.
    Those ids live only in a class NAME, so `runner.py`'s import-time assert (and the test above)
    cannot see them, and a class-wired id could silently collide with a `BaseRule`'s. A `# noqa`
    keys on the id string, so a collision means one suppression quietly disarms two rules.

    The subject is every `TestCE<NNN>` class in the suite, where EVERY rule surfaces — a `BaseRule`
    has one testing it and a class-wired rule IS one. So two rules claiming one number show up as
    two classes claiming it, whichever kind either is. (A single id appearing both in `ALL_RULES`
    and on a test class is the normal shape, not a collision.)

    **Scans `tests/lint_tests/` as well as this file, which is the whole point after the split.**
    It used to read `Path(__file__)` alone, and every one of those classes has moved — so the
    narrowed version would have found ZERO ids and reported no collisions, forever. The
    `len(class_ids) > 20` guard below is what caught that, and it is why the guard is there.

    Boundary: a `BaseRule` with no `TestCE<NNN>` class of its own contributes nothing here and
    could still collide unseen — which is itself a reason to keep the convention.
    """
    sources = [*sorted((Path(__file__).parent / "lint_tests").glob("*.py")), Path(__file__)]
    class_ids = [
        m.group(1)
        for path in sources
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and (m := re.match(r"^Test(CE\d+)", node.name))
    ]

    assert len(class_ids) > 20, f"only {len(class_ids)} TestCE classes found — has the convention moved?"
    duplicates = sorted({i for i in class_ids if class_ids.count(i) > 1})
    assert not duplicates, (
        f"{duplicates} are claimed by more than one rule. A `# noqa` keys on the id, so one "
        "suppression would disarm both — renumber the newer one. Note `runner.py`'s import-time "
        "assert cannot see this: it covers ALL_RULES, and roughly a third of the CE rules are "
        "`@pytest.mark.lint` classes rather than BaseRules."
    )
