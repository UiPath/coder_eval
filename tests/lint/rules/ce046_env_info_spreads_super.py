"""CE046: a ``get_environment_info`` override must spread the base result.

``Agent.get_environment_info`` emits the ``system_prompt_semantics`` run marker.
An override that returns a bare dict drops it, and every run of that agent is
silently mis-bucketed as pre-marker.

Fires on any method named ``get_environment_info`` defined directly in a class
body that neither

  * calls ``super().get_environment_info()`` (the override contract), nor
  * references ``self.system_prompt_semantics`` (the base itself, which emits
    the marker directly).

``# noqa: CE046`` only if an agent genuinely must not record the marker.

Rationale: .claude/notes/lint-rules.md § CE046
"""

import ast

from tests.lint.rules.base import BaseRule


_METHOD = "get_environment_info"


def _spreads_super(node: ast.FunctionDef) -> bool:
    """True if the body calls ``super().get_environment_info()`` anywhere."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == _METHOD
            and isinstance(sub.value, ast.Call)
            and isinstance(sub.value.func, ast.Name)
            and sub.value.func.id == "super"
        ):
            return True
    return False


def _emits_marker_directly(node: ast.FunctionDef) -> bool:
    """True if the body reads ``self.system_prompt_semantics`` (the base itself)."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "system_prompt_semantics"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "self"
        ):
            return True
    return False


class EnvInfoSpreadsSuper(BaseRule):
    id = "CE046"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if (
                isinstance(stmt, ast.FunctionDef)
                and stmt.name == _METHOD
                and not _spreads_super(stmt)
                and not _emits_marker_directly(stmt)
            ):
                self.violation(
                    stmt,
                    f"{node.name}.{_METHOD} returns without spreading "
                    "super().get_environment_info(), so the system_prompt_semantics run "
                    "marker is dropped and every run of this agent is silently mis-bucketed as "
                    "pre-marker. Spread the base: {**super().get_environment_info(), ...}",
                )
        self.generic_visit(node)
