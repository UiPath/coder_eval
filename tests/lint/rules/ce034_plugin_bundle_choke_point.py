"""CE034: agents must only ever see staged plugin bundles, never raw plugin paths.

The orchestrator's `_setup` is the single choke point where `agent.plugins[].path`
is rewritten from the raw source checkout (which carries graders, RESOLUTION.md
reference answers, and test fixtures) to a verified file-allowlisted bundle
(coder_eval.plugin_bundle.stage_agent_plugins). Every agent construction site in
the orchestrator sits downstream of that rewrite, so a future edit that removes
the staging call — or moves `_create_agent()` above it — silently hands the agent
a pointer to its own answer key again.

This rule guards the choke point (mirroring CE033's container_perms guard on the
docker branch): in `coder_eval/orchestrator.py`, any method that calls
`self._create_agent()` must call `self._stage_agent_plugin_bundles()` first
(earlier in the same method body). It deliberately does NOT try to prove global
dataflow — new out-of-orchestrator agent-construction paths must route through
the same staging seam and extend this rule.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_ORCHESTRATOR_FILE = re.compile(r"[/\\]coder_eval[/\\]orchestrator\.py$")

_STAGE_CALL = "_stage_agent_plugin_bundles"
_CREATE_CALL = "_create_agent"


def _self_method_calls(func: ast.AsyncFunctionDef | ast.FunctionDef, name: str) -> list[ast.Call]:
    """All `self.<name>(...)` calls inside ``func`` (including awaited ones)."""
    calls: list[ast.Call] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            calls.append(node)
    return calls


class PluginBundleChokePoint(BaseRule):
    id = "CE034"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._applies = bool(_ORCHESTRATOR_FILE.search(filepath))

    def _check_method(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        creates = _self_method_calls(node, _CREATE_CALL)
        if not creates:
            return
        stages = _self_method_calls(node, _STAGE_CALL)
        first_create = min(c.lineno for c in creates)
        if not stages or min(s.lineno for s in stages) > first_create:
            self.violation(
                creates[0],
                f"self.{_CREATE_CALL}() without a preceding self.{_STAGE_CALL}() in the same method: "
                "the agent would receive raw plugin paths (graders / RESOLUTION.md answer key) instead of "
                "the verified bundle. Stage plugins through coder_eval.plugin_bundle before creating the agent.",
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._applies:
            self._check_method(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._applies:
            self._check_method(node)
        self.generic_visit(node)
