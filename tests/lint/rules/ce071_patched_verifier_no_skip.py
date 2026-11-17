"""CE071: A locally patched binary-reward verifier must not skip or xfail.

Flags ``pytest.skip`` / ``pytest.importorskip`` / ``pytest.xfail`` calls and
``pytest.mark.skip`` / ``skipif`` / ``xfail`` marks. A binary verifier's runner
writes reward 1 when pytest exits 0, so a skipped check counts as a pass.

Scope: every ``tasks/samples/skillsbench/*/verifier/test_outputs.py`` whose first
line starts with ``PATCHED_MARKER``. The test runs this rule together with CE005,
which catches the broad ``except Exception: pass`` swallow. ``dialogue-parser`` is
out of scope because it is unpatched and its runner scores passed / total, so a
skip lowers its reward instead of passing it.

BLIND SPOT: aliased imports (``from pytest import skip``) and dynamically built
calls are not resolved.

Add ``# noqa: CE071`` on the offending line for a deliberate exception.

Rationale: .claude/notes/lint-rules.md § CE071
"""

import ast

from tests.lint.rules.base import BaseRule


PATCHED_MARKER = "# Modified from benchflow-ai/skillsbench@"
_OUTCOME_CALLS = frozenset({"skip", "importorskip", "xfail"})
_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})


def _is_pytest_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "pytest"


def _is_pytest_outcome_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _OUTCOME_CALLS and _is_pytest_name(func.value)


def _is_pytest_skip_mark(node: ast.Attribute) -> bool:
    owner = node.value
    return (
        node.attr in _SKIP_MARKS
        and isinstance(owner, ast.Attribute)
        and owner.attr == "mark"
        and _is_pytest_name(owner.value)
    )


class PatchedVerifierNoSkip(BaseRule):
    id = "CE071"

    def visit_Call(self, node: ast.Call) -> None:
        if _is_pytest_outcome_call(node):
            self.violation(node, "patched binary verifier must fail, not skip or xfail")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_pytest_skip_mark(node):
            self.violation(node, "patched binary verifier must not carry a skip or xfail mark")
        self.generic_visit(node)
