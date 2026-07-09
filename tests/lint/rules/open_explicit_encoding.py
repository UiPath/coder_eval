"""CE011: ``open()`` in text mode must pass ``encoding=``.

Without an explicit encoding, ``open()`` falls back to
``locale.getpreferredencoding(False)`` — ``cp1252`` on a default Windows
install. A YAML / JSON / Markdown file containing UTF-8 bytes then mojibakes
or raises ``UnicodeDecodeError``.

Same root cause as CE008 (``read_text`` / ``write_text``) and CE010
(``subprocess.run``). The fix is always the same: pass
``encoding="utf-8"``.

Scope:
- Flags ``open(...)`` calls without ``encoding=`` when the mode is text
  (the default, or any mode that does not contain ``'b'``).
- Skips ``'rb'`` / ``'wb'`` / ``'ab'`` and any other byte-mode that
  contains ``'b'`` — those return raw bytes, so encoding does not apply.
- Skips calls whose mode is a non-literal expression (dynamic mode is rare
  in this codebase; let those be reviewed manually).

Add ``# noqa: CE011`` on the call line if a locale-dependent decode is
truly desired (no current callers — placeholder for the rare case).
"""

import ast

from tests.lint.rules.base import BaseRule


def _mode_arg(node: ast.Call) -> str | None:
    """Return the literal ``mode`` argument to ``open()`` if available.

    ``open()`` signature: ``open(file, mode='r', buffering=-1, encoding=None, ...)``.
    Mode is positional index 1 or kwarg ``mode``. Returns ``None`` if the
    mode is dynamic (a Name/Call/etc., not a string literal).
    """
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        return node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


class OpenExplicitEncoding(BaseRule):
    id = "CE011"

    def visit_Call(self, node: ast.Call) -> None:
        # Match the bare ``open(...)`` builtin — not ``foo.open(...)`` /
        # ``Path.open(...)`` (those have their own paths; CE002/CE008 cover
        # the blocking-IO and read_text concerns separately).
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = _mode_arg(node)
            # Default mode is 'r' (text). Treat unknown / dynamic mode as
            # text too — better to flag a false positive (suppressible with
            # an inline noqa) than miss a real cp1252 surprise.
            is_byte_mode = mode is not None and "b" in mode
            if not is_byte_mode:
                has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
                if not has_encoding:
                    self.violation(
                        node,
                        "open() in text mode without encoding= is locale-dependent (cp1252 on Windows). "
                        "Pass encoding='utf-8' for deterministic decoding (or 'rb'/'wb' for byte mode).",
                    )
        self.generic_visit(node)
