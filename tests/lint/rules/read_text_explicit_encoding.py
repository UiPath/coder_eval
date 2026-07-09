"""CE008: ``Path.read_text()`` calls must pass ``encoding=``.

Without an explicit encoding, ``read_text()`` falls back to the platform locale
(``locale.getpreferredencoding(False)``), which is ``utf-8`` on modern Linux/macOS
but typically ``cp1252`` on Windows. A judge transcript with a UTF-8 ``é`` then
mojibakes on Windows and silently corrupts downstream comparison/scrubbing logic.

The fix is always the same: pass ``encoding="utf-8"`` (or another explicit
encoding) so the read is deterministic across platforms.

Add ``# noqa: CE008`` on the call line if a locale-dependent read is truly
desired (no current callers — placeholder for the rare case).
"""

import ast

from tests.lint.rules.base import BaseRule


class ReadTextExplicitEncoding(BaseRule):
    id = "CE008"

    def visit_Call(self, node: ast.Call) -> None:
        # Match ``<expr>.read_text(...)`` regardless of receiver shape.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
            for kw in node.keywords:
                if kw.arg == "encoding":
                    break
            else:
                self.violation(
                    node,
                    "Path.read_text() without encoding= is locale-dependent (cp1252 on Windows). "
                    "Pass encoding='utf-8' for deterministic decoding.",
                )
        self.generic_visit(node)
