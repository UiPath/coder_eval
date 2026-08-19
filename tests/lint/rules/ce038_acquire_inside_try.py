"""CE038: in an async context manager, the acquire must sit INSIDE the try.

An ``@contextlib.asynccontextmanager`` whose shape is::

    held = await acquire()          # <-- outside
    try:
        yield
    finally:
        release(held)

leaks whenever a cancellation lands on that ``await``. This is not hypothetical
and ``asyncio.shield`` does not fix it: shield protects the *inner* task, so the
awaiting coroutine still receives ``CancelledError``, propagates it out of
``__aenter__``, and never reaches the ``finally`` — while the shielded work goes
right on completing. The motivating bug held a reference directory at mode 000
with no matching restore: unreadable for the rest of the run, plus a stale
registry entry that poisoned the next window on the same path. The comment above
it claimed shielding prevented exactly that.

The fix is mechanical — move the acquire inside the ``try`` and initialise the
name to an empty value before it::

    held = []
    try:
        held = await acquire()
        yield
    finally:
        release(held)

Fires only when all four conditions hold, so it stays specific: the function is
an async context manager, a name is bound by an ``await`` in the statement
immediately preceding a ``try``, that ``try`` has a ``finally``, and the
``finally`` references the bound name. ``# noqa: CE038`` if the acquire genuinely
cannot fail partway.
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
