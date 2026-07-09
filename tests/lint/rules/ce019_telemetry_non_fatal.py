"""CE019: telemetry public functions must wrap their body in try/except Exception.

Telemetry is a side-channel that must NEVER raise into a run. Each public
function in ``src/coder_eval/telemetry.py`` therefore has to guard its whole
body with a broad ``try/except Exception`` (or a bare ``except``). This rule
turns that design invariant into mechanical enforcement.

Scope: only files ending in ``telemetry.py`` under ``src/coder_eval/``, and only
the four core public functions (the explicit allowlist below). Private helpers
(``_coerce_props``) and the ``track_command`` decorator are out of scope — their
only side-effecting call is ``track_event``, which is itself guarded.

A function body is "guarded" when, after skipping a leading docstring, ``global``
declaration, and any leading no-op guard clauses (``if ...: return``/``pass``),
the remaining body is a single ``try`` whose handlers include a broad
``except Exception`` or a bare ``except``.

Allowlist caveat (mirrors CE002): the function-name set is explicit — extend it
when a new public telemetry function is added.
"""

import ast

from tests.lint.rules.base import BaseRule


_GUARDED_FUNCTIONS = frozenset({"init_telemetry", "track_event", "flush_telemetry", "shutdown_telemetry"})


def _file_is_in_scope(filepath: str) -> bool:
    norm = filepath.replace("\\", "/")
    return "/coder_eval/" in norm and norm.endswith("telemetry.py")


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _is_noop_guard(node: ast.stmt) -> bool:
    """A leading ``if ...: return``/``pass`` guard clause (no work done)."""
    return (
        isinstance(node, ast.If)
        and not node.orelse
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return | ast.Pass)
    )


def _is_broad_handler(handler: ast.excepthandler) -> bool:
    """True for a bare ``except`` or ``except Exception``."""
    if not isinstance(handler, ast.ExceptHandler):
        return False
    if handler.type is None:  # bare except
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def _body_is_fully_guarded(body: list[ast.stmt]) -> bool:
    # Skip leading docstring / global / no-op guards.
    rest = [n for n in body if not (_is_docstring(n) or isinstance(n, ast.Global) or _is_noop_guard(n))]
    if len(rest) != 1 or not isinstance(rest[0], ast.Try):
        return False
    return any(_is_broad_handler(h) for h in rest[0].handlers)


class TelemetryNonFatal(BaseRule):
    id = "CE019"

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if (
            _file_is_in_scope(self.filepath)
            and node.name in _GUARDED_FUNCTIONS
            and not _body_is_fully_guarded(node.body)
        ):
            self.violation(
                node,
                f"telemetry function '{node.name}' must wrap its body in try/except Exception — "
                "telemetry must never raise into a run",
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)
