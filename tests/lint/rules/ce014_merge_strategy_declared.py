"""CE014: every ``list``-typed field on a config-merge root model must declare
an explicit ``MergeField(strategy=...)``.

The declarative merge engine reads a per-field strategy off the Pydantic
``FieldInfo`` (``coder_eval.models.merge_strategy.merge_strategy_of``), falling
back to a type-aware default: nested ``BaseModel`` / free-form ``dict`` -> ``deep``;
``list`` / scalar -> ``replace``. A ``list`` is the one type whose default
(``replace``) is easy to mean-otherwise (``append``) — so a list field that
silently keeps the ``replace`` default when it meant ``append`` is a latent
resolution bug. This rule forces the choice to be explicit.

Scope is the set of model classes the engine actually feeds through
``merge_layers`` / ``resolve_root`` — the three ``-D``-reachable roots, the
sandbox sub-models reached by deep merge, AND the two models merged outside the
``-D`` roots (``TaskDefinition`` for ``pre_run``/``post_run``, ``SimulationConfig``
for ``constraints``). Scoping by class name (not file) keeps the rule pinned to
the engine's real roots and avoids flagging unrelated list fields that happen to
share a file (e.g. ``PreRunCommand`` in ``tasks.py``).

Nested-``BaseModel`` and free-form ``dict`` fields are allowed to rely on the
type-aware ``deep`` default (a plain ``Field(...)`` is fine) — the nested-replace
regression is structurally impossible for them. An explicit ``replace`` override
on a list is permitted and visible; this rule only requires the annotation, not
a particular strategy.

Add ``# noqa: CE014`` on the field line for an intentional exception.
"""

import ast

from tests.lint.rules.base import BaseRule


# The model classes the merge engine resolves. Mirrors config_merge's roots
# (``ALLOWED_OVERRIDE_ROOTS`` -> agent/run_limits/sandbox model types) plus the
# two models merged via direct ``merge_layers`` calls in experiment.py. Files
# are a cheap pre-filter; the class-name set is the authoritative scope.
_SCOPED_PATHS = (
    "src/coder_eval/models/agent_config.py",
    "src/coder_eval/models/sandbox.py",
    "src/coder_eval/models/limits.py",
    "src/coder_eval/models/tasks.py",
)

_MERGE_ROOT_CLASSES = frozenset(
    {
        # agent root (union members + base for subclass-only fields)
        "BaseAgentConfig",
        "ClaudeCodeAgentConfig",
        "CodexAgentConfig",
        # run_limits root
        "RunLimits",
        # sandbox root + sub-models reached by deep merge
        "SandboxConfig",
        "DockerDriverConfig",
        "PythonEnvConfig",
        "NodeEnvConfig",
        "ResourceLimits",
        # merged outside the -D roots via direct merge_layers calls
        "TaskDefinition",  # pre_run / post_run
        "SimulationConfig",  # constraints
    }
)


def _is_list_annotation(node: ast.expr | None) -> bool:
    """True for ``list[...]``, a bare ``list``, ``Annotated[list[...], ...]``, or any
    union (``list[...] | None``) containing one of those."""
    if isinstance(node, ast.Name) and node.id == "list":  # bare ``list``
        return True
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == "list":
            return True
        if node.value.id == "Annotated":
            # Annotated[X, ...] — X is the first element of the subscript.
            inner = node.slice
            first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
            return _is_list_annotation(first)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_list_annotation(node.left) or _is_list_annotation(node.right)
    return False


def _is_mergefield_call(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MergeField"


class MergeStrategyDeclared(BaseRule):
    id = "CE014"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        normalized = filepath.replace("\\", "/")
        self._active = any(normalized.endswith(p) for p in _SCOPED_PATHS)

    def check(self, tree: ast.AST) -> list:  # type: ignore[override]
        if not self._active:
            return []
        # Only class-body fields (Pydantic model fields) on the merge-root
        # classes — skip module-level constants, in-function annotations, and
        # non-root classes that merely share a scoped file.
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in _MERGE_ROOT_CLASSES:
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and _is_list_annotation(stmt.annotation)
                    and not _is_mergefield_call(stmt.value)
                ):
                    name = stmt.target.id if isinstance(stmt.target, ast.Name) else "<field>"
                    self.violation(
                        stmt,
                        (
                            f"list field '{name}' on a config-merge root model must declare an explicit "
                            "MergeField(strategy='append'|'replace') — a bare Field() leaves it at the "
                            "ambiguous 'replace' default. (Nested-model/dict fields may use plain Field.)"
                        ),
                    )
        return self.violations
