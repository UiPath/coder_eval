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
* **Registry + the criterion union, no recursion.** The four top-level models are
  registered explicitly; the members of the ``SuccessCriterion`` discriminated
  union are enumerated programmatically (a task author writes them directly, the
  guide already claims to be their reference, and the set is closed/enumerable).
  In neither case do we recurse into *nested* models (``AgentConfig``,
  ``SandboxConfig``, the criteria's own sub-models, …) — that would silently
  expand the commitment to dozens of models nobody signed up for. Enumerating the
  union (not recursing) is what makes "the next criterion-semantics change cannot
  ship undocumented" mechanical: a new criterion, or a new field on one, fails
  until it is documented or exempted.
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

import ast
from pathlib import Path

from pydantic import BaseModel

from coder_eval.models import Dataset, RunLimits, SimulationConfig, TaskDefinition
from coder_eval.models import criteria as _criteria_module


_GUIDE = "docs/TASK_DEFINITION_GUIDE.md"


def _union_literal_criterion_names() -> list[str]:
    """Class names in the ``SuccessCriterion = Annotated[A | B | ...]`` literal.

    Read STATICALLY from ``criteria.py`` source, not from the runtime union
    object. That is the whole point: a plugin's ``coder_eval.plugins`` hook can
    inject its OWN criterion into the runtime union (and even into the module
    namespace) at load time — the ``uipath`` SDK adds a ``CliCalledCriterion`` —
    and such a criterion is documented in its plugin's own repo, not this guide.
    A plugin cannot edit this repo's source, so the source literal is the
    authoritative, contamination-proof list of the criteria THIS repo ships.
    """
    tree = ast.parse(Path(_criteria_module.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SuccessCriterion" for t in node.targets
        ):
            # value is ``Annotated[<Name | Name | ...>, Field(...)]``; every
            # criterion class name ends in "Criterion" (Annotated/Field/discriminator
            # string do not), so this selects exactly the union members.
            return [n.id for n in ast.walk(node.value) if isinstance(n, ast.Name) and n.id.endswith("Criterion")]
    raise AssertionError("SuccessCriterion union assignment not found in criteria.py")


def _criterion_models() -> list[type[BaseModel]]:
    """The in-tree ``SuccessCriterion`` member models, resolved from the source union.

    Names come from the source literal (see :func:`_union_literal_criterion_names`),
    then each is resolved to its class via ``getattr`` on the criteria module. New
    in-tree criteria are covered automatically (they're added to the union literal);
    plugin-injected criteria are never named in the source, so they are excluded
    even though they exist as runtime attributes.
    """
    return [getattr(_criteria_module, name) for name in _union_literal_criterion_names()]


# Models the project commits to documenting, paired with the doc page that owns
# their field reference. The four top-level models are explicit; the criterion
# union members are enumerated so a new criterion (or field) can't ship undocumented.
DOCUMENTED_MODELS: list[tuple[type[BaseModel], str]] = [
    (TaskDefinition, _GUIDE),
    (RunLimits, _GUIDE),
    (Dataset, _GUIDE),
    (SimulationConfig, _GUIDE),
    *[(m, _GUIDE) for m in _criterion_models()],
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
