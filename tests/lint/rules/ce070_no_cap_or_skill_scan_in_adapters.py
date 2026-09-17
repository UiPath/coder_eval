"""CE070: agent adapters neither count run caps nor scan for skills.

The defect: every harness once carried its own copy of both jobs. Five adapters each
counted a turn cap in their own unit (Claude Code's SDK turns, Codex and Antigravity
visible turns, Pi's ``turn_start`` steps, OpenCode's ``step_start``), so one
``max_turns`` meant five budgets. Four adapters each walked ``agent.plugins`` for
``SKILL.md`` with their own depth rules, so one plugin path loaded skills on some
harnesses and nothing on others. Now the ``TurnMonitor`` owns every cap and budget
(an adapter only honours the ``should_stop`` reason), the collector is the one writer
of ``tool_calls_exhausted``, and ``orchestration/plugin_staging.py`` is the one
scanner (an adapter only delivers ``plugin_root``).

Fires, in files under ``src/coder_eval/agents/``, on any name, attribute, keyword,
parameter or ``from``-import alias spelled ``max_turns``, ``max_tool_calls``,
``max_turns_reached``, ``max_turns_hit``, ``expected_turns``, ``tool_calls_exhausted``,
``RunLimits`` or ``expand_env_vars``, and on the string literal ``"SKILL.md"``. The identifiers are the
sensor because the cap has one owner, the flag one writer and staging one scanner: an
adapter that needs any of them is re-growing a copy. A substring is not a match, so
``_is_max_turns_result`` and the SDK's ``"error_max_turns"`` stay legal.

Blind spots: a reducer that counts ``ToolEndEvent``s under another name, and a scanner
that globs ``*.md`` or builds the file name from parts. Both are listed in
``.claude/harness-candidates.md``.
"""

import ast

from tests.lint.rules._model_ctor import AGENTS_ROOT
from tests.lint.rules.base import BaseRule


BANNED_IDENTIFIERS = frozenset(
    {
        "max_turns",
        "max_tool_calls",
        "max_turns_reached",
        "max_turns_hit",
        "expected_turns",
        "tool_calls_exhausted",
        "RunLimits",
        "expand_env_vars",
    }
)
_SKILL_FILE = "SKILL.md"
_FIX = (
    "caps and budgets belong to the TurnMonitor (honour the should_stop reason) and skill discovery to "
    "orchestration/plugin_staging.py (deliver plugin_root)"
)


class NoCapOrSkillScanInAdapters(BaseRule):
    id = "CE070"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_agents = bool(AGENTS_ROOT.search(filepath))

    def _flag(self, node: ast.AST, name: str) -> None:
        if self._in_agents and name in BANNED_IDENTIFIERS:
            self.violation(node, f"architectural violation: '{name}' in an agent adapter — {_FIX}")

    def visit_Name(self, node: ast.Name) -> None:
        self._flag(node, node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._flag(node, node.attr)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg is not None:
            self._flag(node, node.arg)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._flag(node, node.arg)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._flag(node, alias.name)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._in_agents and node.value == _SKILL_FILE:
            self.violation(node, f"architectural violation: '{_SKILL_FILE}' scanned in an agent adapter — {_FIX}")
        self.generic_visit(node)
