"""CE030 — models the project commits to documenting must have no undocumented fields.

Every defect this docs overhaul fixed was the same failure: a doc claim that no
longer matched (or never matched) the code. P0 and P1 were literally "a Pydantic
field the user must set, documented nowhere." CE030 is the sensor that makes that
class impossible to reintroduce: for a small, explicit registry of user-facing
models, every field must appear in the model's doc page as inline code, or be
listed in ``EXEMPT`` with a reason it is not user-authored.

Design choices, each load-bearing:

* **Allowlist, not denylist.** A new field on a registered model that is neither
  documented nor exempted *fails* — which is the point. Adding a user-facing field
  now forces a doc update or a reasoned exemption in the same change.
* **Explicit registry, no recursion.** Only the four registered models are
  checked; nested models (``AgentConfig``, ``SandboxConfig``, criteria, …) are NOT
  walked. Walking them would silently expand the documentation commitment to
  dozens of models nobody signed up for.
* **Inline-code match, deliberately simple.** A field counts as documented when
  its bare name appears wrapped in Markdown inline-code backticks anywhere in the
  doc. This is a floor, not a proof — a field name that appears in an unrelated
  context (e.g. a
  common word like ``rows``) can pass spuriously. Accepted: the rule exists to
  catch *entirely undocumented* fields, and a fuzzier "documented in the right
  section" rule invites false passes that erode trust in the gate.

Like CE027/CE029, this is intentionally NOT a ``BaseRule`` registered in
``tests/lint/runner.py`` (that runner is AST-only over ``.py`` files); it reasons
over Markdown and is wired as ``tests/test_custom_lint.py::TestCE030DocSchemaParity``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from coder_eval.models import Dataset, RunLimits, SimulationConfig, TaskDefinition


# Models the project commits to documenting, paired with the doc page that owns
# their field reference. Keep this list SHORT and explicit — every entry is a
# standing documentation obligation.
DOCUMENTED_MODELS: list[tuple[type[BaseModel], str]] = [
    (TaskDefinition, "docs/TASK_DEFINITION_GUIDE.md"),
    (RunLimits, "docs/TASK_DEFINITION_GUIDE.md"),
    (Dataset, "docs/TASK_DEFINITION_GUIDE.md"),
    (SimulationConfig, "docs/TASK_DEFINITION_GUIDE.md"),
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
