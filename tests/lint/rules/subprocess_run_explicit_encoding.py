"""CE010: ``subprocess.run(..., text=True, ...)`` must pass ``encoding=``.

Without an explicit encoding, the text-mode decoder falls back to
``locale.getpreferredencoding(False)`` — ``cp1252`` on a default Windows
install. Command output containing UTF-8 bytes then mojibakes or raises
``UnicodeDecodeError`` mid-run.

The fix is always the same: pass ``encoding="utf-8"`` (with
``errors="replace"`` on agent-facing / diagnostic paths where stdout may
include adversarial bytes; strict on setup-critical paths so corruption
surfaces as a real failure).

Covers ``subprocess.run``/``call``/``check_output``/``check_call`` and the
``subprocess.Popen`` constructor — all share the same text-mode decode
contract. Also covers the legacy ``universal_newlines=True`` alias.

Add ``# noqa: CE010`` on the call line if a locale-dependent decode is
truly desired (no current callers — placeholder for the rare case).
"""

import ast

from tests.lint.rules.base import BaseRule


def _is_subprocess_run(func: ast.expr) -> bool:
    """Return True for ``subprocess.run`` and the related entrypoints.

    Matches ``subprocess.run``/``call``/``check_output``/``check_call`` and
    the ``subprocess.Popen`` constructor — all accept the same ``text`` /
    ``encoding`` kwargs and decode stdout/stderr the same way.
    """
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"run", "call", "check_output", "check_call", "Popen"}:
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "subprocess"


class SubprocessRunExplicitEncoding(BaseRule):
    id = "CE010"

    def visit_Call(self, node: ast.Call) -> None:
        if _is_subprocess_run(node.func):
            text_mode = False
            has_encoding = False
            for kw in node.keywords:
                if kw.arg in {"text", "universal_newlines"} and isinstance(kw.value, ast.Constant) and kw.value.value:
                    text_mode = True
                if kw.arg == "encoding":
                    has_encoding = True
            if text_mode and not has_encoding:
                self.violation(
                    node,
                    "subprocess.run(..., text=True, ...) without encoding= is locale-dependent (cp1252 on Windows). "
                    "Pass encoding='utf-8' for deterministic decoding.",
                )
        self.generic_visit(node)
