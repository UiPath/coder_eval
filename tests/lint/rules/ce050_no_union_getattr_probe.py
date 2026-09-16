"""CE050: no untyped ``getattr`` probe for a field of a discriminated union.

Fires on ``getattr(<obj>, "<literal>", ...)`` in ``src/coder_eval/`` where the
literal is a field declared by a member of ``SuccessCriterion``,
``TemplateSource`` or ``ApiRoute`` (derived from the models at collection time,
minus common names such as ``type``) AND ``<obj>`` is named like a criterion,
template source or route. Pyright cannot see the string, so a rename turns the
probe into a permanent ``None``.

Known limit, deliberate: ``command``/``tool``/``prompt`` are too common to flag
alone (agents probe SDK event objects for them), so the rule scopes to
criterion-shaped receiver names and an unusual receiver name is uncovered.

Fix with ``isinstance`` narrowing; ``# noqa: CE050`` for a probe that really is
duck-typed across unrelated objects.

Rationale: .claude/notes/lint-rules.md § CE050
"""

import ast
import re

from tests.lint.rules.base import BaseRule


def _union_field_names() -> set[str]:
    """Field names declared by any member of the tracked unions.

    Derived from the models rather than hardcoded: a hardcoded list is the same
    class of staleness the rule exists to prevent. Names shared with ordinary
    object attributes (``type``, ``description``, ``weight`` …) are excluded —
    they are not what a rename would break, and flagging them would only teach
    people to noqa the rule.
    """
    import typing

    from coder_eval.models import ApiRoute, SuccessCriterion, TemplateSource

    common = {"type", "description", "weight", "pass_threshold", "model", "path"}

    def _members(annotation: object) -> list[object]:
        """Flatten ``Annotated[Union[...], Field(discriminator=...)]`` to its members.

        The unions are all discriminated, so a bare ``__args__`` yields
        ``(Union[...], FieldInfo)`` and the model classes are one level deeper —
        which silently produced an EMPTY field set, i.e. a rule that could never
        fire. Recurse instead.
        """
        args = typing.get_args(annotation)
        if not args:
            return [annotation]
        out: list[object] = []
        for arg in args:
            out.extend(_members(arg) if typing.get_args(arg) else [arg])
        return out

    names: set[str] = set()
    for union in (SuccessCriterion, TemplateSource, ApiRoute):
        for member in _members(union):
            names |= set(getattr(member, "model_fields", {}))
    assert names, "CE050 derived no union field names — the rule could never fire"
    return names - common


# Receivers that name a member of one of the tracked unions. See the module
# docstring for why the rule is scoped this way rather than on the field alone.
_RECEIVER = re.compile(r"^(c|cr|crit|criterion|source|template_source|route|api_route)$|(_criterion|_source|_route)$")


def _union_receiver(node: ast.expr) -> bool:
    """Whether the probed object is named like a union member."""
    if isinstance(node, ast.Name):
        return bool(_RECEIVER.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_RECEIVER.search(node.attr))
    return False


class NoUnionGetattrProbe(BaseRule):
    id = "CE050"

    # `(^|sep)` so a repo-relative path is in scope too; see CE054.
    _SRC_PATH = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
    _fields: set[str] | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._SRC_PATH.search(filepath))
        if self._in_scope and NoUnionGetattrProbe._fields is None:
            NoUnionGetattrProbe._fields = _union_field_names()

    def visit_Call(self, node: ast.Call) -> None:
        self._check(node)
        self.generic_visit(node)

    def _check(self, node: ast.Call) -> None:
        if not self._in_scope:
            return
        if not isinstance(node.func, ast.Name) or node.func.id != "getattr" or len(node.args) < 2:
            return
        key = node.args[1]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return
        if key.value not in (NoUnionGetattrProbe._fields or set()):
            return
        if not _union_receiver(node.args[0]):
            return
        self.violation(
            node,
            f"getattr(..., {key.value!r}) probes a discriminated-union field with an untyped "
            + "string. pyright cannot see it, so a rename turns this into a silent no-op that "
            + "returns None forever — and it cannot reach members that express the same "
            + "capability under a different field. Narrow with isinstance instead.",
        )
