"""CE031 — behavior-driving config fields must be consumed somewhere in ``src/``.

For every model in ``CONSUMED_MODELS``, each field must appear as an **attribute
access** (``x.field``) somewhere under ``src/``, or be listed in ``EXEMPT`` with a
reason it is consumed only via serialization.

Register only behavior-driving models. A field read only through ``model_dump()`` is
not an attribute access, so a serialization or telemetry model, or ``TaskDefinition``,
would report live fields as dead.

BLIND SPOT: attribute names are not tied to a model. A dead field is masked by a
same-named attribute read anywhere under ``src/``. This causes false negatives, never
false positives.

Wired as ``tests/test_custom_lint.py::TestCE031DeadConfigFields``, not the AST runner,
because it reasons over the whole ``src/`` tree at once.

Rationale: .claude/notes/lint-rules.md § CE031
"""

from __future__ import annotations

import ast
from pathlib import Path

from pydantic import BaseModel

from coder_eval.models import Dataset, RunLimits, SimulationConfig


# Behavior-driving config models whose fields MUST be read by name to do anything.
# Deliberately excludes serialization/telemetry models and TaskDefinition (fields
# round-tripped through model_dump would false-positive under the attribute rule).
CONSUMED_MODELS: list[type[BaseModel]] = [
    SimulationConfig,
    RunLimits,
    Dataset,
]

# Fields legitimately consumed only via serialization (not a by-name attribute
# read), with the reason. Empty today; an entry here is a promise the field IS
# used, just not through attribute access. Every entry must name a real field.
EXEMPT: dict[str, dict[str, str]] = {}


def consumed_attr_names(src_root: Path) -> set[str]:
    """Every attribute name read anywhere under ``src/`` (``x.attr`` -> ``attr``)."""
    names: set[str] = set()
    for py in src_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def dead_config_fields(model: type[BaseModel], consumed: set[str], exempt: dict[str, str]) -> list[str]:
    """Fields of ``model`` neither read as an attribute in ``src/`` nor exempted."""
    return [name for name in model.model_fields if name not in exempt and name not in consumed]


def find_dead_config_fields(src_root: Path) -> dict[str, list[str]]:
    """Map ``Model`` name to its dead (unconsumed) fields, for every registered model."""
    consumed = consumed_attr_names(src_root)
    findings: dict[str, list[str]] = {}
    for model in CONSUMED_MODELS:
        exempt = EXEMPT.get(model.__name__, {})
        dead = dead_config_fields(model, consumed, exempt)
        if dead:
            findings[model.__name__] = dead
    return findings
