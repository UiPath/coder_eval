"""CE069 — the run-limit and agent-field tables are generated from the models and agent classes.

Each in-tree agent class declares a ``HarnessContract`` and, when it honors the tool
lists, a ``ToolNameMap``. Those declarations, with ``RunLimits``, are the single source
of truth for what a run limit or a uniform agent field means on a harness; a
hand-written table beside them drifts the first time a row flips. ``write()`` renders
three Markdown tables into ``docs/agents/HARNESS_PARITY.md`` between their marker
pairs, ``make parity-table`` calls it, and CE069 (``check()``) re-renders and diffs
against disk. A ``RunLimits`` field with no cell rule fails the render, so a new limit
cannot ship undocumented.

Columns are the in-tree kinds in ``AgentKind`` order. A plugin kind registered in the
test process is not rendered: its run record carries its own contract.

Wired as ``tests/test_custom_lint.py::TestCE069HarnessParityTable``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import CANONICAL_TOOL_NAMES, AgentKind, HarnessContract, RunLimits, UsageGranularity
from coder_eval.plugins import ensure_plugins_loaded
from tests.lint.doc_indexes import _replace_between
from tests.lint.generated import diff_all, write_all


CONTRACT_START = "<!-- harness-contract:start -->"
CONTRACT_END = "<!-- harness-contract:end -->"
TOOLS_START = "<!-- harness-tools:start -->"
TOOLS_END = "<!-- harness-tools:end -->"
RUN_LIMITS_START = "<!-- harness-run-limits:start -->"
RUN_LIMITS_END = "<!-- harness-run-limits:end -->"
_DOC = Path("docs/agents/HARNESS_PARITY.md")


def _kinds() -> list[AgentKind]:
    return [kind for kind in AgentKind if kind is not AgentKind.UNKNOWN]


def _agent_class(kind: AgentKind) -> type:
    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None, f"in-tree kind {kind!r} is not registered"
    return registration.agent_class


def _cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, frozenset):
        return ", ".join(sorted(str(v) for v in value))
    return str(value)


def _row(label: str, cells: list[str]) -> str:
    return f"| {label} | " + " | ".join(cells) + " |"


def render_table() -> str:
    """One row per ``HarnessContract`` field, one column per in-tree kind."""
    kinds = _kinds()
    contracts = [_agent_class(kind).contract for kind in kinds]
    lines = [_row("field", [str(k) for k in kinds]), _row("---", ["---"] * len(kinds))]
    lines += [
        _row(f"`{field}`", [_cell(getattr(contract, field)) for contract in contracts])
        for field in HarnessContract.model_fields
    ]
    return "\n".join(lines)


_USAGE_UNIT: dict[UsageGranularity, str] = {
    UsageGranularity.GENERATION: "model generation",
    UsageGranularity.STEP: "agent-loop step",
    UsageGranularity.TURN: "communicate() call",
}


def _budget_cell(contract: HarnessContract) -> str:
    unit = _USAGE_UNIT[contract.usage_granularity]
    return f"TurnMonitor; usage reported per {unit}; overshoot ≤ one {unit} + calls in flight"


_RUN_LIMIT_CELLS: dict[str, Callable[[HarnessContract], str]] = {
    "max_tool_calls": lambda c: (
        "TurnMonitor at the should_stop poll, main-thread resolved tool calls"
        if c.cooperative_stop
        else "not polled (never fires)"
    ),
    "max_turns": lambda c: (
        "TurnMonitor at the should_stop poll, when main-thread model turn N+1 starts"
        if c.counts_model_turns
        else "rejected at resolution"
    ),
    "expected_tool_calls": lambda _c: "orchestrator, cumulative visible tool calls, warns only",
    "expected_turns": lambda c: (
        "orchestrator, cumulative model turns (TurnMonitor count), warns only"
        if c.counts_model_turns
        else "rejected at resolution"
    ),
    "task_timeout": lambda _c: "orchestrator, agent-agnostic",
    "turn_timeout": lambda _c: "agent watchdog (see Timeouts)",
    "max_input_tokens": _budget_cell,
    "max_output_tokens": _budget_cell,
    "max_total_tokens": _budget_cell,
    "max_usd": lambda c: (
        _budget_cell(c)
        + ("; priced by the harness" if c.reports_cost else "; needs a priced agent.model (checked at resolution)")
    ),
    "count_cached_input": lambda _c: "TurnMonitor bucket rule",
    "count_cache_creation": lambda _c: "TurnMonitor bucket rule",
    "stop_early": lambda c: "cooperative should_stop" if c.cooperative_stop else "rejected at resolution",
    "stop_early_gate_threshold": lambda _c: "armed gate",
}


def render_run_limits_table(fields: list[str] | None = None) -> str:
    """One row per ``RunLimits`` field in declaration order, one column per in-tree kind.

    Raises:
        KeyError: a field has no cell rule in ``_RUN_LIMIT_CELLS``.
    """
    kinds = _kinds()
    contracts = [_agent_class(kind).contract for kind in kinds]
    rows = fields if fields is not None else list(RunLimits.model_fields)
    missing = [field for field in rows if field not in _RUN_LIMIT_CELLS]
    if missing:
        raise KeyError(f"RunLimits field(s) {missing} have no cell rule in _RUN_LIMIT_CELLS")
    lines = [_row("limit", [str(k) for k in kinds]), _row("---", ["---"] * len(kinds))]
    lines += [_row(f"`{field}`", [_RUN_LIMIT_CELLS[field](contract) for contract in contracts]) for field in rows]
    return "\n".join(lines)


def render_tool_table() -> str:
    """One row per canonical tool name, one column per kind that maps tool names."""
    mapped = [(kind, names) for kind in _kinds() if (names := _agent_class(kind).tool_names) is not None]
    lines = [_row("tool", [str(k) for k, _ in mapped]), _row("---", ["---"] * len(mapped))]
    lines += [
        _row(f"`{name}`", [", ".join(f"`{n}`" for n in tool_names.names[name]) or "none" for _, tool_names in mapped])
        for name in sorted(CANONICAL_TOOL_NAMES)
    ]
    return "\n".join(lines)


def _rendered_files(repo_root: Path) -> dict[Path, str]:
    doc = repo_root / _DOC
    text = _replace_between(
        doc.read_text(encoding="utf-8"), RUN_LIMITS_START, RUN_LIMITS_END, render_run_limits_table()
    )
    text = _replace_between(text, CONTRACT_START, CONTRACT_END, render_table())
    return {doc: _replace_between(text, TOOLS_START, TOOLS_END, render_tool_table())}


def write(repo_root: Path) -> list[Path]:
    """Regenerate the three tables in place. Returns the files touched."""
    return write_all(_rendered_files(repo_root))


def check(repo_root: Path) -> dict[str, str]:
    """Unified diff per file whose generated content differs from disk (empty = clean)."""
    return diff_all(_rendered_files(repo_root))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    for p in write(root):
        print(f"wrote {p}")
