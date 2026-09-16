"""CE069 — the agent-field contract tables are generated from the agent classes.

Each in-tree agent class declares a ``HarnessContract`` and, when it honors the tool
lists, a ``ToolNameMap``. Those declarations are the single source of truth for what a
uniform agent field means on a harness; a hand-written table beside them drifts the
first time a row flips. ``write()`` renders two Markdown tables into
``docs/agents/HARNESS_PARITY.md`` between their marker pairs, ``make parity-table``
calls it, and CE069 (``check()``) re-renders and diffs against disk.

Columns are the in-tree kinds in ``AgentKind`` order. A plugin kind registered in the
test process is not rendered: its run record carries its own contract.

Wired as ``tests/test_custom_lint.py::TestCE069HarnessParityTable``.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import CANONICAL_TOOL_NAMES, AgentKind, HarnessContract
from coder_eval.plugins import ensure_plugins_loaded
from tests.lint.doc_indexes import _replace_between
from tests.lint.generated import diff_all, write_all


CONTRACT_START = "<!-- harness-contract:start -->"
CONTRACT_END = "<!-- harness-contract:end -->"
TOOLS_START = "<!-- harness-tools:start -->"
TOOLS_END = "<!-- harness-tools:end -->"
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
    text = _replace_between(doc.read_text(encoding="utf-8"), CONTRACT_START, CONTRACT_END, render_table())
    return {doc: _replace_between(text, TOOLS_START, TOOLS_END, render_tool_table())}


def write(repo_root: Path) -> list[Path]:
    """Regenerate both tables in place. Returns the files touched."""
    return write_all(_rendered_files(repo_root))


def check(repo_root: Path) -> dict[str, str]:
    """Unified diff per file whose generated content differs from disk (empty = clean)."""
    return diff_all(_rendered_files(repo_root))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    for p in write(root):
        print(f"wrote {p}")
