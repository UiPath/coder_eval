"""CE009: Pydantic input-config models must declare ``extra='forbid'``.

Without it a misspelled YAML key is silently dropped; ``extra='forbid'`` rejects the
key at load time and names the field.

Scope: the model modules that parse task or experiment YAML, listed in
``_SCOPED_PATHS``. ``models/results.py`` and ``models/telemetry.py`` are out of scope
(task.json round-trips keep forward-compat fields); result-shaped classes in
``models/experiment.py`` carry per-class ``# noqa: CE009`` for the same reason.

A class is compliant when its own body declares
``model_config = ConfigDict(..., extra='forbid', ...)``, when a base defined in the
SAME file declares it, or when it does not directly extend ``BaseModel``.

BLIND SPOT: a class that extends a non-compliant same-file base passes; only that base
is flagged. Base chains are not followed across files.

Add ``# noqa: CE009`` on the class statement line for an intentional exception.

Rationale: .claude/notes/lint-rules.md § CE009
"""

import ast

from tests.lint.rules.base import BaseRule


# Forward-slash suffixes; the filepath is normalized first, so Windows paths match too.
_SCOPED_PATHS = (
    "src/coder_eval/models/tasks.py",
    "src/coder_eval/models/criteria.py",
    "src/coder_eval/models/mutations.py",
    "src/coder_eval/models/experiment.py",
    "src/coder_eval/models/templates.py",
    "src/coder_eval/models/sandbox.py",
    "src/coder_eval/models/agent_config.py",
    "src/coder_eval/models/limits.py",
)


def _is_basemodel_base(b: ast.expr) -> bool:
    """``class X(BaseModel)`` or ``class X(BaseModel, ABC)`` — any direct BaseModel arg."""
    if isinstance(b, ast.Name) and b.id == "BaseModel":
        return True
    # Allow attribute form ``pydantic.BaseModel`` (unused in this codebase but defensive).
    return isinstance(b, ast.Attribute) and b.attr == "BaseModel"


def _declares_extra_forbid(class_body: list[ast.stmt]) -> bool:
    """True iff the class body assigns ``model_config = ConfigDict(..., extra='forbid', ...)``."""
    for stmt in class_body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not (
            len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "model_config"
        ):
            continue
        call = stmt.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "ConfigDict"):
            continue
        for kw in call.keywords:
            if kw.arg == "extra" and isinstance(kw.value, ast.Constant) and kw.value.value == "forbid":
                return True
    return False


class YamlModelsForbidExtras(BaseRule):
    id = "CE009"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        # Class name → declares extra="forbid"; filled before the visit, read by same-file descendants.
        self._extra_forbid_by_class: dict[str, bool] = {}
        normalized = filepath.replace("\\", "/")
        self._active = any(normalized.endswith(p) for p in _SCOPED_PATHS)

    def check(self, tree: ast.AST) -> list:  # type: ignore[override]
        if not self._active:
            return []
        # First pass: record which classes declare it, so same-file descendants inherit compliance.
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._extra_forbid_by_class[node.name] = _declares_extra_forbid(node.body)
        # Second pass: flag classes with neither their own declaration nor a compliant same-file base.
        self.visit(tree)
        return self.violations

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        descends_from_basemodel = any(_is_basemodel_base(b) for b in node.bases)
        if _declares_extra_forbid(node.body):
            self.generic_visit(node)
            return
        # A compliant same-file base is enough: pydantic inherits model_config.
        for b in node.bases:
            if isinstance(b, ast.Name) and self._extra_forbid_by_class.get(b.id, False):
                self.generic_visit(node)
                return
        # Only direct BaseModel subclasses are flagged; see the module BLIND SPOT.
        if descends_from_basemodel:
            self.violation(
                node,
                (
                    f"Pydantic input model '{node.name}' must declare "
                    "`model_config = ConfigDict(extra='forbid')` so typos in YAML "
                    "fields surface as errors instead of being silently dropped."
                ),
            )
        self.generic_visit(node)
