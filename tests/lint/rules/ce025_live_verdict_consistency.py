"""CE025: a criterion's ``live_stop_polarities`` and ``live_verdict`` must agree.

The early-stop live-verdict contract (``criteria/base.py``) pairs two members on
every ``BaseCriterion`` subclass:

- ``live_stop_polarities: ClassVar[frozenset[str]]`` — the polarities the
  criterion can decide from a PARTIAL, mid-run trajectory. Empty (the base
  default) means "not observable mid-run", so the criterion can never arm
  early-stop.
- ``live_verdict(...)`` — the method that actually reads the partial trajectory
  and returns ``"pass"``/``"fail"``/``"undecided"``.

The two must move together: a non-empty ``live_stop_polarities`` without a
``live_verdict`` override arms a criterion whose base ``live_verdict`` always
returns ``"undecided"`` (it can never stop — a silent dead arm); a
``live_verdict`` override without a non-empty ``live_stop_polarities`` writes
decision logic the arming path can never reach (dead code that reads as
supported). Either drift is a mechanically detectable bug, so flag it.

Scoped to ``src/coder_eval/criteria/`` and exempts ``criteria/base.py`` (which
legitimately declares the empty default alongside the default ``live_verdict``).

Non-empty detection: ``frozenset({...})`` / ``frozenset([...])`` with a
non-empty literal is non-empty; bare ``frozenset()`` is empty; a ``frozenset``
call over a non-literal argument (or any other RHS) is conservatively treated as
non-empty.

Add ``# noqa: CE025`` on the class line for a deliberate exception.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CRITERIA_DIR = re.compile(r"[/\\]coder_eval[/\\]criteria[/\\]")
_BASE_FILE = re.compile(r"[/\\]coder_eval[/\\]criteria[/\\]base\.py$")


def _is_frozenset(func: ast.expr) -> bool:
    return isinstance(func, ast.Name) and func.id == "frozenset"


def _polarities_nonempty(value: ast.expr) -> bool:
    """Whether the RHS of a ``live_stop_polarities`` assignment is non-empty.

    ``frozenset(<literal>)`` reads the literal's element count; bare
    ``frozenset()`` is empty; anything else is conservatively non-empty so a
    computed value is never mistaken for a dead arm.
    """
    if isinstance(value, ast.Call) and _is_frozenset(value.func):
        if not value.args:
            return False
        arg = value.args[0]
        if isinstance(arg, ast.Set | ast.List | ast.Tuple):
            return len(arg.elts) > 0
        if isinstance(arg, ast.Dict):
            return len(arg.keys) > 0
        # Non-literal argument (a name, comprehension, …) — conservatively non-empty.
        return True
    # Not a frozenset() call at all — conservatively treat the declaration as non-empty.
    return True


class LiveVerdictConsistency(BaseRule):
    id = "CE025"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._active = bool(_CRITERIA_DIR.search(filepath)) and not bool(_BASE_FILE.search(filepath))

    def check(self, tree: ast.AST) -> list:  # type: ignore[override]
        if not self._active or not isinstance(tree, ast.Module):
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._check_class(node)
        return self.violations

    def _check_class(self, node: ast.ClassDef) -> None:
        has_polarities = False
        polarities_nonempty = False
        has_live_verdict = False
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "live_verdict":
                has_live_verdict = True
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "live_stop_polarities" and stmt.value is not None:
                    has_polarities = True
                    polarities_nonempty = _polarities_nonempty(stmt.value)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "live_stop_polarities":
                        has_polarities = True
                        polarities_nonempty = _polarities_nonempty(stmt.value)

        if has_polarities and polarities_nonempty and not has_live_verdict:
            self.violation(
                node,
                "declares a non-empty `live_stop_polarities` but no `live_verdict` override; "
                "the base `live_verdict` always returns 'undecided', so this arms a criterion "
                "that can never stop (add a `live_verdict` override or clear the polarities).",
            )
        elif has_live_verdict and not (has_polarities and polarities_nonempty):
            self.violation(
                node,
                "overrides `live_verdict` but declares no non-empty `live_stop_polarities`; "
                "the arming path can never reach this decision logic (declare the polarities "
                "it supports or drop the override).",
            )
