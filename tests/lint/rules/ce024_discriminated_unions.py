"""CE024: unions of ``type: Literal``-tagged models in ``coder_eval/models/`` must
declare a discriminator.

A bare ``A | B | C`` union of tagged Pydantic models validates via smart-union:
input missing its ``type`` tag (or carrying a typo'd one) silently coerces to
whichever variant happens to fit, instead of raising a crisp discriminator
error. That is exactly how the ``SuccessCriterion`` union shipped — tag-less
criterion dicts coerced to the structurally-nearest variant — until it was
wrapped in ``Annotated[..., Field(discriminator="type")]``
(``models/mutations.py:48`` is the canonical compliant shape).

Flagged: a module-level assignment (plain, PEP 695 ``type X = ...``, or
annotated) whose value is a ``|``-chain (or a ``Union[...]`` subscript) of
two or more names that ALL refer to classes
defined in the same file with a ``type: Literal[...]`` field — including such
a union inside an ``Annotated[...]`` that carries no discriminator metadata.

Compliant (not flagged): the union as the first element of ``Annotated[...]``
whose metadata contains ``Field(discriminator=...)`` or a ``Discriminator(...)``
call (the callable form used by ``CriterionResultUnion`` in
``models/results.py``). Unions with any untagged or imported member are out of
scope (same-file conservatism, mirroring CE009's documented trade).

Add ``# noqa: CE024`` on the assignment line for a deliberate exception.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_MODELS_DIR = re.compile(r"[/\\]coder_eval[/\\]models[/\\]")


def _is_tagged_class(node: ast.ClassDef) -> bool:
    """True iff the class body declares ``type: Literal[...]``."""
    for stmt in node.body:
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "type"
            and isinstance(stmt.annotation, ast.Subscript)
            and isinstance(stmt.annotation.value, ast.Name)
            and stmt.annotation.value.id == "Literal"
        ):
            return True
    return False


def _union_member_names(node: ast.expr) -> list[str] | None:
    """Member names if ``node`` is a union of plain names, else None.

    Handles both the ``A | B | C`` BinOp chain (recursively flattened, so the
    parenthesized multi-line style works) and the ``Union[A, B, C]`` subscript.
    Returns None as soon as any member is not a bare name.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _union_member_names(node.left)
        right = _union_member_names(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Union":
        elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        names: list[str] = []
        for elt in elts:
            if not isinstance(elt, ast.Name):
                return None
            names.append(elt.id)
        return names
    if isinstance(node, ast.Name):
        return [node.id]
    return None


def _has_discriminator_metadata(metadata: list[ast.expr]) -> bool:
    """True iff any metadata element is ``Field(discriminator=...)`` or ``Discriminator(...)``."""
    for m in metadata:
        if not isinstance(m, ast.Call):
            continue
        func = m.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == "Discriminator":
            return True
        if name == "Field" and any(kw.arg == "discriminator" for kw in m.keywords):
            return True
    return False


class DiscriminatedUnions(BaseRule):
    id = "CE024"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._active = bool(_MODELS_DIR.search(filepath))
        self._tagged_classes: set[str] = set()

    def check(self, tree: ast.AST) -> list:  # type: ignore[override]
        if not self._active or not isinstance(tree, ast.Module):
            return []
        # First pass: collect same-file classes declaring a `type: Literal[...]` tag.
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_tagged_class(node):
                self._tagged_classes.add(node.name)
        # Second pass: only module-level statements can define a union alias —
        # plain assignment, PEP 695 `type X = ...` (the prevailing style in
        # models/), or an annotated assignment.
        for stmt in tree.body:
            value = stmt.value if isinstance(stmt, ast.Assign | ast.TypeAlias | ast.AnnAssign) else None
            if value is not None:
                self._check_alias_value(stmt, value)
        return self.violations

    def _check_alias_value(self, stmt: ast.stmt, value: ast.expr) -> None:
        # Annotated[...] form: inspect the first element + metadata.
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name) and value.value.id == "Annotated":
            if not isinstance(value.slice, ast.Tuple) or len(value.slice.elts) < 2:
                return
            inner, *metadata = value.slice.elts
            if self._is_bare_tagged_union(inner) and not _has_discriminator_metadata(metadata):
                self._flag(stmt)
            return
        if self._is_bare_tagged_union(value):
            self._flag(stmt)

    def _is_bare_tagged_union(self, node: ast.expr) -> bool:
        names = _union_member_names(node)
        return names is not None and len(names) >= 2 and all(n in self._tagged_classes for n in names)

    def _flag(self, stmt: ast.stmt) -> None:
        self.violation(
            stmt,
            "bare union of `type: Literal`-tagged models; wrap it in "
            "`Annotated[..., Field(discriminator='type')]` so a missing/typo'd tag raises a "
            "discriminator error instead of smart-union coercing (see models/mutations.py).",
        )
