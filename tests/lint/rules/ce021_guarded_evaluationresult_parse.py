"""CE021: ``EvaluationResult.model_validate_json(...)`` must be inside a guarding try.

A malformed ``task.json`` raises ``ValidationError`` / ``JSONDecodeError`` (both
``ValueError``); the caller must degrade to a persisted synthetic ERROR record,
not crash. Pattern: ``batch.py::_load_completed_result``.

The call must sit lexically inside a ``try`` BODY whose handlers catch
``ValueError``, ``Exception``, ``BaseException``, or bare ``except``; a tuple
naming one of them counts. An unrelated type (``except OSError``) does not
guard, and neither do the ``else`` / ``except`` / ``finally`` bodies.

Scope: ``EvaluationResult.model_validate_json`` spelled on that bare name only;
other models are not matched.

Add ``# noqa: CE021`` on the call line only if a caller genuinely must not catch.

Rationale: .claude/notes/lint-rules.md § CE021
"""

import ast

from tests.lint.rules.base import BaseRule


_GUARD_TYPES = frozenset({"ValueError", "Exception", "BaseException"})


def _is_guarding_handler(handler: ast.excepthandler) -> bool:
    """True for ``except`` (bare), ``except ValueError/Exception/BaseException``,
    or a tuple ``except (..., ValueError, ...)`` naming one of those types."""
    if not isinstance(handler, ast.ExceptHandler):
        return False
    if handler.type is None:  # bare except
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in _GUARD_TYPES
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id in _GUARD_TYPES for elt in handler.type.elts)
    return False


def _is_evaluationresult_parse(func: ast.expr) -> bool:
    """True for ``EvaluationResult.model_validate_json(...)``."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "model_validate_json"
        and isinstance(func.value, ast.Name)
        and func.value.id == "EvaluationResult"
    )


class GuardedEvaluationResultParse(BaseRule):
    id = "CE021"

    # Traversal state: True while the current node is lexically inside a try
    # body protected by a guarding handler. Declared here so the state is
    # statically visible rather than materialized via a getattr default.
    _guarded: bool = False

    def visit_Try(self, node: ast.Try) -> None:
        # Only the try's BODY is guarded by its handlers. The else clause runs
        # after the body succeeds and an exception raised there propagates
        # UNCAUGHT (it is not protected by this try's except), exactly like the
        # handler/finally bodies — so all three are visited with the guard
        # restored to the surrounding context's level.
        prev = self._guarded
        guarded_here = any(_is_guarding_handler(h) for h in node.handlers)
        self._guarded = prev or guarded_here
        for stmt in node.body:
            self.visit(stmt)
        # else/except/finally bodies are not protected by this try's handlers.
        self._guarded = prev
        for stmt in node.orelse:
            self.visit(stmt)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_evaluationresult_parse(node.func) and not self._guarded:
            self.violation(
                node,
                "EvaluationResult.model_validate_json must be inside a try/except ValueError so a "
                "malformed/stale-image task.json degrades to a synthetic ERROR record instead of "
                "crashing — mirror batch.py:_load_completed_result. Add `# noqa: CE021` only if a "
                "caller genuinely must not catch.",
            )
        self.generic_visit(node)
