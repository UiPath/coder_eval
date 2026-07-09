"""CE005: Broad except clauses must not silently swallow errors.

An `except Exception:` or bare `except:` that neither re-raises nor surfaces
the error hides bugs and makes failures undetectable.

A block is considered handled if any of the following is true:
  - contains a `raise` statement
  - calls a method named: exception, error, warning, critical, warn, handleError
  - the exception is bound (`except Exception as e`) AND `e` is referenced
    anywhere in the handler body (covers console.print(e), return Result(error=e),
    _helper(exc), logger.debug(msg, e), etc.) — note: this is a heuristic;
    indirect references like `x = str(type(e))` also pass even if `e` is not
    actually surfaced to the caller

Add `# noqa: CE005` on the `except` line to suppress a known-safe case
(e.g. intentional try/fallback patterns where the exception is irrelevant).
"""

import ast

from tests.lint.rules.base import BaseRule


_LOG_ATTRS = {"exception", "error", "warning", "critical", "warn", "handleError"}


def _body_handles_error(handler: ast.ExceptHandler) -> bool:
    body = handler.body

    # If the exception is bound and the name appears anywhere in the body,
    # the caller is explicitly using it — not silently swallowing it.
    if handler.name:
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and node.id == handler.name:
                    return True

    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_ATTRS:
                return True
    return False


def _is_broad_type(t: ast.expr | None) -> bool:
    """A broad clause is bare `except:` or one whose type set includes Exception."""
    if t is None:
        return True
    if isinstance(t, ast.Name) and t.id == "Exception":
        return True
    # `except (Exception, OtherError):` is a Tuple node — broad if any element is Exception.
    if isinstance(t, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "Exception" for elt in t.elts)
    return False


class NoSilentExcept(BaseRule):
    id = "CE005"

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if _is_broad_type(node.type) and not _body_handles_error(node):
            self.violation(
                node,
                "broad except silently swallows errors — add logging, re-raise, or use a specific exception type",
            )
        self.generic_visit(node)
