"""CE031 — behavior-driving config fields must be consumed somewhere in ``src/``.

A Pydantic field on a config model that users set in a task YAML but that no code
ever reads is *dead config*: it silently does nothing, and the author has no way
to know. This is what ``SimulationConfig.parallel_trials`` was — documented, set
in a shipped task YAML, defaulting to ``True``, and read nowhere (trial
concurrency is entirely ``--max-parallel``'s job). CE031 makes that class
impossible to reintroduce for a small, explicit registry of models.

"Consumed" here means the field name appears as an **attribute access**
(``x.field``) anywhere under ``src/`` — the consumption contract for a
*behavior-driving* config: the orchestrator/validators must read the field by
name for it to have any effect. A field read only via ``model_dump()`` /
serialization is NOT caught by this definition, which is exactly why the registry
is restricted to behavior models (``SimulationConfig``, ``RunLimits``,
``Dataset``) and does **not** include serialization/telemetry models or the
sprawling ``TaskDefinition`` (whose fields are largely round-tripped through
``model_dump`` in the dataset expander).

Known floor (documented, accepted): attribute names collide across models — if
``RunLimits`` and ``SimulationConfig`` both declare ``max_turns`` and only one is
read by name, both count as consumed. Collisions cause **false negatives** (a dead
field masked by a same-named live one elsewhere), never false positives, so the
rule can never wrongly break the build. An ``EXEMPT`` map covers any field that is
legitimately consumed only via serialization, with a reason.

Like CE027 through CE030, this is not a ``BaseRule`` in the AST runner (that runner walks
one file at a time and reports line-level violations; this rule reasons over the
*whole* ``src/`` tree at once). It is wired as
``tests/test_custom_lint.py::TestCE031DeadConfigFields``.
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
# read), with the reason. An entry here is a promise the field IS used, just not
# through attribute access — or, for a deprecated field, that leaving it inert is
# deliberate. Every entry must name a real field.
EXEMPT: dict[str, dict[str, str]] = {
    "RunLimits": {
        "expected_turns": (
            "deprecated and intentionally inert: efficiency is scored in wall-clock "
            "seconds against a line derived from run history. Kept accepted because "
            "RunLimits forbids extras and ~930 task YAMLs still declare it; dropped "
            "in a later minor."
        ),
    },
}


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
