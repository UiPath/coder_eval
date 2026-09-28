"""CE030 — models the project commits to documenting must have no undocumented fields.

``DOCUMENTED_MODELS`` is the registry of tracked models, each paired with its doc
page. Every field of a registered model must appear in that page as Markdown inline
code, or be listed in ``EXEMPT`` with a reason it is not user-authored. A new field
that is neither fails ``make lint``; that is the intent.

Nested models (``SandboxConfig``, ``CliMatch``, criteria) are NOT walked; each
registration is a standing documentation obligation.

BLIND SPOT: a field name used as inline code in an unrelated context (e.g. ``rows``)
passes. Models sharing field names (``RecordedCli`` / ``CliResponse``) share coverage,
so a green gate is not per-model coverage.

Wired as ``tests/test_custom_lint.py::TestCE030DocSchemaParity``, not the AST runner:
it reasons over Markdown.

Rationale: .claude/notes/lint-rules.md § CE030
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from coder_eval.models import CliResponse, Dataset, RecordedCli, RunLimits, SimulationConfig, TaskDefinition


# Models the project commits to documenting, paired with the doc page that owns
# their field reference. Keep this list SHORT and explicit — every entry is a
# standing documentation obligation.
DOCUMENTED_MODELS: list[tuple[type[BaseModel], str]] = [
    (TaskDefinition, "docs/TASK_DEFINITION_GUIDE.md"),
    (RunLimits, "docs/TASK_DEFINITION_GUIDE.md"),
    (Dataset, "docs/TASK_DEFINITION_GUIDE.md"),
    (SimulationConfig, "docs/TASK_DEFINITION_GUIDE.md"),
    (RecordedCli, "docs/TASK_DEFINITION_GUIDE.md"),
    (CliResponse, "docs/TASK_DEFINITION_GUIDE.md"),
]

# Fields deliberately absent from the user docs, with the reason each is not
# user-authored. An entry here is a promise: this field is set by the framework,
# not by a task author, so it needs no doc. Every entry must name a real field on
# its model (see test_exemptions_reference_real_fields).
EXEMPT: dict[str, dict[str, str]] = {
    "TaskDefinition": {
        "suite_id": "set by the dataset expander on expanded row-tasks; not user-authored",
        "row_id": "set by the dataset expander from Dataset.id_field; not user-authored",
    },
}


def undocumented_fields(model: type[BaseModel], doc_text: str, exempt: dict[str, str]) -> list[str]:
    """Field names of ``model`` that appear neither as inline code in ``doc_text`` nor in ``exempt``."""
    missing: list[str] = []
    for name in model.model_fields:
        if name in exempt:
            continue
        if f"`{name}`" in doc_text:
            continue
        missing.append(name)
    return missing


def find_undocumented_fields(repo_root: Path) -> dict[str, list[str]]:
    """Map ``"Model (doc_path)"`` to its undocumented field names, for every registered model."""
    findings: dict[str, list[str]] = {}
    for model, doc_rel in DOCUMENTED_MODELS:
        doc_path = repo_root / doc_rel
        doc_text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""
        exempt = EXEMPT.get(model.__name__, {})
        missing = undocumented_fields(model, doc_text, exempt)
        if missing:
            findings[f"{model.__name__} ({doc_rel})"] = missing
    return findings
