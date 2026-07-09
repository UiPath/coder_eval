"""CE007: ``.max_turns`` / ``.task_timeout`` / ``.turn_timeout`` are no longer top-level fields.

Phase 1 of the unify-run-limits refactor (2026-05-12) removed these from
``TaskDefinition``, ``ExperimentDefaults``, and ``ExperimentVariant``. They
live under ``run_limits`` now. This rule blocks reintroduction by flagging
``<task-like>.<banned>`` attribute access in core code.

Pattern matched: an ``Attribute`` read or write whose attribute name is one of
the three banned names AND whose immediate prefix matches a known task-config
identifier (``task``, ``self.task``, ``resolved_task``, ``expanded_task``,
``variant``, ``defaults``, ``experiment.defaults``).

Allow-list — these prefixes are NOT flagged:
- ``simulation`` / ``sim`` / ``sim_config`` / ``config.simulation`` /
  ``self.config.simulation`` / ``self.task.simulation`` → SimulationConfig.
- ``criterion`` / ``agent_judge`` / single-letter ``c`` inside
  ``coder_eval/criteria/agent_judge.py`` → AgentJudgeCriterion.
- File ``coder_eval/models/limits.py`` → defining the fields themselves.
- File ``coder_eval/orchestration/experiment.py`` inside ``_apply_cli_overrides``
  reading ``config.<field>`` → BatchRunConfig.
"""

from __future__ import annotations

import ast
import os

from tests.lint.rules.base import BaseRule


_BANNED_FIELDS = {"max_turns", "task_timeout", "turn_timeout"}

# Prefixes (last attribute name OR variable id) that mean "this is a task /
# experiment-layer config object" and we should flag the access.
_TASK_PREFIXES = {"task", "resolved_task", "expanded_task", "variant", "defaults"}

# Prefixes that map to non-task config objects (SimulationConfig, criteria,
# BatchRunConfig). Reads on these are legitimate.
_ALLOWED_PREFIXES = {
    "simulation",
    "sim",
    "sim_config",
    "criterion",
    "agent_judge",
    "rl",
    "run_limits",
}


def _attribute_chain(node: ast.AST) -> list[str]:
    """Return the dotted chain ending at this Attribute, innermost first.

    For ``self.task.max_turns`` the chain is ``["self", "task", "max_turns"]``.
    For ``config.simulation.max_turns`` the chain is
    ``["config", "simulation", "max_turns"]``. Returns ``[]`` for nodes whose
    base isn't a plain Name (e.g. function calls, subscripts).
    """
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return []


class NoTopLevelRunLimitsAccess(BaseRule):
    id = "CE007"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr not in _BANNED_FIELDS:
            self.generic_visit(node)
            return

        chain = _attribute_chain(node)
        # Need at least [base, banned] — skip anything weirder (calls, subscripts).
        if len(chain) < 2:
            self.generic_visit(node)
            return

        # Strip leading "self" — `self.task.max_turns` should be treated as
        # `task.max_turns` for prefix matching.
        head = chain[:-1]
        if head and head[0] == "self":
            head = head[1:]
        if not head:
            self.generic_visit(node)
            return

        # File-level allow-lists (resolve once per visit).
        norm_path = os.path.normpath(self.filepath).replace("\\", "/")
        if "src/coder_eval/models/limits.py" in norm_path:
            return
        # `BatchRunConfig` reads happen exclusively in experiment.py's
        # _apply_cli_overrides; allow them.
        if "src/coder_eval/orchestration/experiment.py" in norm_path and head == ["config"]:
            self.generic_visit(node)
            return
        # AgentJudgeCriterion field reads happen in agent_judge.py.
        if "src/coder_eval/criteria/agent_judge.py" in norm_path and head[-1] in {"criterion", "agent_judge", "c"}:
            self.generic_visit(node)
            return

        # The penultimate name decides whether it's a task or an allowed
        # other-config: e.g. `config.simulation.max_turns` → "simulation".
        immediate = head[-1]
        if immediate in _ALLOWED_PREFIXES:
            self.generic_visit(node)
            return

        if immediate in _TASK_PREFIXES:
            self.violation(
                node,
                f"'.{node.attr}' is no longer a top-level field on TaskDefinition / "
                f"ExperimentDefaults / ExperimentVariant — use run_limits.{node.attr} "
                "(see c/2026-05-12-unify-run-limits.md).",
            )

        self.generic_visit(node)
