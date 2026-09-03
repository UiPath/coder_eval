"""CE047: every ``environment_info`` key that is READ must also be WRITTEN.

``EvaluationResult.environment_info`` is a ``dict[str, Any]`` bag, so nothing —
not pydantic, not pyright — connects the site that writes a key to the site that
reads it back. A reader whose writer was never added (or was later removed) is
silently inert: ``.get("k")`` returns ``None``, the guard takes its early return,
and the feature reports success while doing nothing.

The motivating case: ``verify_reference_unchanged`` read
``environment_info.get("reference_digest")`` to refuse a re-grade whose answer key
had changed. Nothing anywhere wrote that key — a whole-tree grep found exactly one
occurrence, the read itself. The anti-cheat guard shipped, was documented in
CLAUDE.md and the user guide as protection, and never fired once. Every automated
gate in the repo was green.

This is deliberately a one-way check. An unread key is ordinary (recorded for a
human or a downstream consumer); an unwritten key is always a bug.

Use ``# noqa: CE047`` for a key genuinely supplied from outside this repo.
"""

import ast
import re
from pathlib import Path

from tests.lint.rules.base import BaseRule


_SRC_ROOT = Path("src/coder_eval")

# Keys written by a consumer outside src/ (the docker container's own capture,
# a plugin) or copied wholesale from another dict. Each needs a reason.
_EXTERNALLY_WRITTEN: dict[str, str] = {}


def _written_keys() -> set[str]:
    """Every string literal assigned into an ``environment_info`` subscript.

    Text-scanned rather than AST-walked across the tree so a write inside any
    module counts regardless of how the dict was reached (``self.result.``,
    ``result.``, a local alias). The rule only needs to know a literal is
    written SOMEWHERE — attributing it precisely would add false positives
    without catching anything more.
    """
    written: set[str] = set()
    pattern = re.compile(r"""environment_info\[\s*["']([\w.-]+)["']\s*\]\s*=""")
    # Also count keys named in a dict literal that becomes environment_info, and
    # the f-string-built provenance keys (`f"graded_by_{key}"`), which no literal
    # scan can resolve — those are covered by the prefix allowance below.
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file in src is not our problem
            continue
        written.update(pattern.findall(text))
    return written


class EnvInfoKeyRoundTrip(BaseRule):
    id = "CE047"

    _SRC_PATH = re.compile(r"[/\\]src[/\\]coder_eval[/\\]")
    _written: set[str] | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._SRC_PATH.search(filepath))
        if self._in_scope and EnvInfoKeyRoundTrip._written is None:
            EnvInfoKeyRoundTrip._written = _written_keys()

    def visit_Call(self, node: ast.Call) -> None:
        self._check_get(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._check_subscript(node)
        self.generic_visit(node)

    def _check_get(self, node: ast.Call) -> None:
        """``<...>.environment_info.get("key")``."""
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            return
        if not _is_env_info(func.value) or not node.args:
            return
        self._require_writer(node, node.args[0])

    def _check_subscript(self, node: ast.Subscript) -> None:
        """``<...>.environment_info["key"]`` in a READ position.

        A write is an ``ast.Store`` context, which is exactly what makes it a
        writer — only loads are checked.
        """
        if not isinstance(node.ctx, ast.Load) or not _is_env_info(node.value):
            return
        self._require_writer(node, node.slice)

    def _require_writer(self, node: ast.AST, key_node: ast.AST) -> None:
        if not self._in_scope:
            return
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            return  # a computed key; nothing to resolve statically
        key = key_node.value
        if key in _EXTERNALLY_WRITTEN or key.startswith("graded_by_"):
            # graded_by_* keys are built with an f-string from a name list, so no
            # literal write exists to find.
            return
        if key in (EnvInfoKeyRoundTrip._written or set()):
            return
        self.violation(
            node,
            f"environment_info key {key!r} is read here but never written anywhere in src/coder_eval. "
            + "A reader with no writer is silently inert — it returns None, the guard takes its early "
            + "return, and the feature reports success while doing nothing (see CE047's docstring for "
            + "the anti-cheat guard that shipped this way). Add the write, or list the key in "
            + "_EXTERNALLY_WRITTEN with the out-of-tree producer that supplies it.",
        )


def _is_env_info(node: ast.AST) -> bool:
    """Whether ``node`` is an ``…​.environment_info`` attribute access."""
    return isinstance(node, ast.Attribute) and node.attr == "environment_info"
