"""CE071: agent adapters and the turn monitor price a turn only through ``pricing.price_turn``.

The defect: five adapters and ``TurnMonitor`` each carried their own copy of the cost
rule on top of ``calculate_cost``. Pi and OpenCode priced a reported ``$0`` from the rate
card, the monitor took it as free, Antigravity and Codex never looked at a report, and
Claude kept a third variant for LiteLLM. So the ``max_usd`` stop and the persisted turn
cost could disagree on the same turn. ``price_turn`` is now the one rule, and an adapter
or the monitor that calls ``calculate_cost`` is re-growing a copy.

Fires, in files under ``src/coder_eval/agents/`` and in
``src/coder_eval/orchestration/turn_monitor.py``, on any name, attribute or
``from``-import alias spelled ``calculate_cost``. Pricing outside a subject turn (the
simulator in ``models/results.py``, ``evaluation/judge_usage.py``) is out of scope.

Blind spot: a copy of the rate arithmetic under another name, e.g. reading
``ModelPricing`` fields directly.
"""

import ast
import re

from tests.lint.rules._model_ctor import AGENTS_ROOT
from tests.lint.rules.base import BaseRule


_TURN_MONITOR = re.compile(r"(?:^|[/\\])orchestration[/\\]turn_monitor\.py$")
_BANNED = "calculate_cost"
_FIX = "price a turn with coder_eval.pricing.price_turn, the one cost rule shared with the max_usd monitor"


class PriceTurnOnly(BaseRule):
    id = "CE071"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath) or _TURN_MONITOR.search(filepath))

    def _flag(self, node: ast.AST, name: str) -> None:
        if self._in_scope and name == _BANNED:
            self.violation(node, f"architectural violation: '{_BANNED}' used to price a turn — {_FIX}")

    def visit_Name(self, node: ast.Name) -> None:
        self._flag(node, node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._flag(node, node.attr)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._flag(node, alias.name)
        self.generic_visit(node)
