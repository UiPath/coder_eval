"""CE046: a ``get_environment_info`` override must spread the base result.

``Agent.get_environment_info`` (agent.py) emits the ``system_prompt_semantics``
run marker from the ClassVar of the same name, so EVERY run — including
out-of-tree SPI agents — records which system-prompt regime built its prompts.
Dashboards read an ABSENT marker as "a run from before the marker existed" and
pool it into a legacy bucket, so an override that returns a bare dict does not
merely omit a key: it silently mis-buckets every one of that agent's runs.

The motivating bug: ``OpenCodeAgent.get_environment_info`` returned
``{"opencode_model": ..., "opencode_pure": ...}`` with no ``super()`` spread, so
no OpenCode run ever carried the marker and no test caught it. Every other agent
(codex/antigravity/claude_code) spreads the base correctly.

The base's docstring already states the contract ("Overrides should spread
``super().get_environment_info()`` rather than returning a bare dict"); this rule
makes it mechanical.

Fires on any method named ``get_environment_info`` defined directly in a class
body that neither

  * calls ``super().get_environment_info()`` (the override contract), nor
  * references ``self.system_prompt_semantics`` (the base itself, which emits
    the marker directly — exempt so the rule does not flag its own source).

``# noqa: CE046`` if an agent genuinely must not record the marker (there is no
such case today).
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
