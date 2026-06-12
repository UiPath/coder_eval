"""CE017: ``coder_eval/models/`` may import ``coder_eval.agents`` / ``coder_eval.plugins``
only lazily (function-local) or under ``TYPE_CHECKING`` — never at module top level.

``models/`` is the pure-Pydantic layer; the dependency arrow runs ``agents`` ->
``models``, not the reverse. The BYOA SPI requires ``models`` to reach the agent
registry for one thing only — registry-driven agent-config dispatch
(``parse_agent_config`` / ``_coerce_agent_config``) — and it does so via
function-local imports so no module-load import cycle forms. A top-level
``from coder_eval.agents...`` / ``import coder_eval.plugins`` in ``models/`` would
re-introduce the cycle CodeQL flagged; this rule keeps that escape hatch deferred.

Allowed (not flagged): imports nested inside a function/method (``col_offset > 0``)
and imports under ``if TYPE_CHECKING:`` (also indented). Flagged: only unconditional
module-level statements (``col_offset == 0``).

Add ``# noqa: CE017`` for a deliberate exception.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_MODELS_DIR = re.compile(r"[/\\]models[/\\]")
_BANNED = re.compile(r"^coder_eval\.(agents|plugins)\b")


class ModelsLazyAgentImports(BaseRule):
    id = "CE017"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_models = bool(_MODELS_DIR.search(filepath))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # col_offset == 0 == an unconditional module-level statement. Function-local
        # and TYPE_CHECKING-guarded imports are indented (col_offset > 0) and allowed.
        if self._in_models and node.col_offset == 0 and node.module and _BANNED.match(node.module):
            self.violation(
                node,
                f"architectural violation: '{node.module}' imported at module level in models/; "
                "import coder_eval.agents/plugins lazily (inside the function) instead.",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_models and node.col_offset == 0:
            for alias in node.names:
                if _BANNED.match(alias.name):
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' imported at module level in models/; "
                        "import coder_eval.agents/plugins lazily (inside the function) instead.",
                    )
        self.generic_visit(node)
