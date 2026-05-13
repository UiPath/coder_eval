"""CE009: Pydantic input-config models must declare ``extra='forbid'``.

Without ``extra='forbid'`` a misspelled YAML key (e.g. ``directry: foo/`` vs
``directory: foo/`` on ``ReferenceSource``, or a typo on a criterion field) is
silently dropped on the floor — the user gets no signal that the key was
ignored. ``extra='forbid'`` rejects unknown keys at load time, surfacing the
typo with a concrete field name in the error message.

Scope (hard-coded path filter):
  - ``src/coder_eval/models/tasks.py``
  - ``src/coder_eval/models/criteria.py``

Result models (``models/results.py``) intentionally use ``extra='allow'`` to
preserve forward-compat fields through ``model_dump_json`` → ``model_validate_json``
round-trips of task.json records, so they are EXEMPT from this rule.

A class is considered compliant when ANY of the following holds:
  1. Its own body declares ``model_config = ConfigDict(..., extra='forbid', ...)``.
  2. It inherits from a base defined in the SAME file whose body declares the
     above (pydantic inherits ``model_config`` from the parent).
  3. It does NOT directly extend ``BaseModel`` in its ``bases`` clause — only
     direct ``BaseModel`` subclasses are flagged. A class extending a non-compliant
     same-file base that itself extends ``BaseModel`` slips through this rule;
     compliance is treated as the base's responsibility (and the base is itself
     flagged). This trade keeps the rule cheap to implement without walking the
     transitive base chain across files.

Add ``# noqa: CE009`` on the class statement line for an intentional exception.
"""

import ast

from tests.lint.rules.base import BaseRule


# Path suffixes are matched against the runner's filepath after normalizing
# backslashes (Windows). Keeping the canonical form forward-slash means the
# rule definition stays platform-neutral while still matching on Windows where
# pathlib hands us native-separator strings.
_SCOPED_PATHS = (
    "src/coder_eval/models/tasks.py",
    "src/coder_eval/models/criteria.py",
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
        # Match: ``model_config = ConfigDict(...)``
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
        # Map of class name → whether it declares extra="forbid". Filled by visit_ClassDef
        # on the first pass; used to satisfy descendants in the same file.
        self._extra_forbid_by_class: dict[str, bool] = {}
        # Path filter: only run on the two scoped files. Normalize backslashes
        # to forward slashes so the suffix match works on Windows runners where
        # pathlib hands the rule native-separator strings.
        normalized = filepath.replace("\\", "/")
        self._active = any(normalized.endswith(p) for p in _SCOPED_PATHS)

    def check(self, tree: ast.AST) -> list:  # type: ignore[override]
        if not self._active:
            return []
        # First pass: record which classes declare extra="forbid" so descendants
        # in the same file can be considered compliant via inheritance.
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._extra_forbid_by_class[node.name] = _declares_extra_forbid(node.body)
        # Second pass: flag classes that neither declare it themselves nor have
        # a base in the same file that does.
        self.visit(tree)
        return self.violations

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Only flag classes that descend from BaseModel (directly or via a base
        # defined elsewhere in the file).
        descends_from_basemodel = any(_is_basemodel_base(b) for b in node.bases)
        # Or transitively: any base name we've seen with extra="forbid" counts.
        # We don't track transitive base→BaseModel chains; we trust the AST + the
        # convention that all model classes in these files derive (eventually)
        # from BaseModel.
        # If this class declares it directly → compliant.
        if _declares_extra_forbid(node.body):
            self.generic_visit(node)
            return
        # If any base is a known-compliant class in the same file → compliant
        # (pydantic inherits model_config).
        for b in node.bases:
            if isinstance(b, ast.Name) and self._extra_forbid_by_class.get(b.id, False):
                self.generic_visit(node)
                return
        # Otherwise: only flag if this class directly extends BaseModel — leaves
        # of the class hierarchy that descend from a known model base are out of
        # scope (their compliance is the base's responsibility).
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
