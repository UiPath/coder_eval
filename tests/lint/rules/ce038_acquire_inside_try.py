"""CE038: in an async context manager, the acquire must sit INSIDE the try.

Fires only when all four hold: the function is decorated ``asynccontextmanager``;
a name is bound by ``<name> = await ...`` in the statement immediately before a
``try``; that ``try`` has a ``finally``; the ``finally`` references the name.

Fix: initialise the name to an empty value and move the acquire inside::

    held = []
    try:
        held = await acquire()
        yield
    finally:
        release(held)

HAZARD: ``asyncio.shield`` does not fix it. It protects the inner task, not the
await, so ``CancelledError`` still leaves ``__aenter__`` before the ``finally``
while the acquire completes.

``# noqa: CE038`` if the acquire genuinely cannot fail partway.

Rationale: .claude/notes/lint-rules.md § CE038
"""

import ast
from itertools import pairwise

from tests.lint.rules.base import BaseRule


def _is_async_cm(node: ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "asynccontextmanager":
            return True
    return False


def _awaited_binding(stmt: ast.stmt) -> str | None:
    """Name bound by ``<name> = await ...``, else None."""
    if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Await):
        return None
    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
        return None
    return stmt.targets[0].id


def _names_in(body: list[ast.stmt]) -> set[str]:
    found: set[str] = set()
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name):
                found.add(sub.id)
    return found


class AcquireInsideTry(BaseRule):
    id = "CE038"

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if _is_async_cm(node):
            self._scan(node.body)
        self.generic_visit(node)

    def _scan(self, body: list[ast.stmt]) -> None:
        for previous, current in pairwise(body):
            if not isinstance(current, ast.Try) or not current.finalbody:
                continue
            bound = _awaited_binding(previous)
            if bound is None or bound not in _names_in(current.finalbody):
                continue
            self.violation(
                previous,
                f"'{bound}' is acquired by an await OUTSIDE the try whose finally releases it; a "
                + "cancellation landing on that await skips the finally while the acquire completes, "
                + "leaking the resource. Move the await inside the try (asyncio.shield does NOT prevent "
                + "this — it protects the inner task, not this await)",
            )
