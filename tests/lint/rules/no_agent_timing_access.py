"""CE006: ``.agent.max_turns`` and ``.agent.turn_timeout`` are no longer fields.

Phase 2 of the agent-timing refactor (2026-05-07) deleted these from
``AgentConfig``. They live on ``TaskDefinition`` (top-level) and as a per-call
argument on ``Agent.communicate(..., max_turns=...)``.

This rule blocks reintroduction. Pattern matched: any AST node of shape
``<x>.agent.<max_turns|turn_timeout>`` — both reads and writes.

Carve-outs:
- ``criterion.max_turns`` / ``criterion.turn_timeout`` — those are real fields
  on ``LLMJudgeCriterion`` / ``AgentJudgeCriterion`` and are not nested under
  ``.agent``, so the AST shape doesn't match.
- ``task.max_turns`` / ``task.turn_timeout`` — top-level fields on
  ``TaskDefinition``; not nested under ``.agent``, doesn't match.

If a legitimate exception arises, suppress with ``# noqa: CE006`` and a
comment explaining why.
"""

import ast

from tests.lint.rules.base import BaseRule


_BANNED_FIELDS = {"max_turns", "turn_timeout"}


class NoAgentTimingAccess(BaseRule):
    id = "CE006"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Match `<anything>.agent.<banned>`: outer Attribute attr is banned,
        # the value chain ends with `.agent`.
        if node.attr in _BANNED_FIELDS:
            inner = node.value
            if isinstance(inner, ast.Attribute) and inner.attr == "agent":
                self.violation(
                    node,
                    f"'.agent.{node.attr}' is no longer a field on AgentConfig — "
                    f"use task.{node.attr} (top-level) or pass max_turns to agent.communicate()",
                )
        self.generic_visit(node)
