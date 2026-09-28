"""CE057: a sidecar module copied beside a generated sandbox shim stays stdlib-only.

``Sandbox._generate_cli_recorders`` writes every module in
``models.sandbox.SIDECAR_MODULES`` beside each ``record_cli`` shim that declares
response rules, and the shim imports it as a sibling where ``coder_eval`` is not
installed. Such a module may import only roots in ``STDLIB_ALLOWED``, an allowlist,
so growing it is a deliberate edit. A relative import always fires.
``from __future__`` gets its own message: drop the line, do not widen the allowlist.

HAZARD: the target set derives from ``SIDECAR_MODULES``, and
``tests/test_custom_lint.py`` asserts it matches a file that exists, so the rule
cannot silently guard zero files.

Rationale: .claude/notes/lint-rules.md § CE057
"""

import ast
import re

from coder_eval.models import SIDECAR_MODULES
from tests.lint.rules.base import BaseRule


class SidecarShimStdlibOnly(BaseRule):
    id = "CE057"

    # Derived from the writer's own list, so moving the module moves the rule
    # with it. A hardcoded second copy would match nothing after such a move and
    # pass vacuously -- guarding zero files while reading as a guarantee.
    # tests/test_custom_lint.py asserts the pattern matches a file that exists.
    _SIDECAR = re.compile(r"[/\\]coder_eval[/\\](?:" + "|".join(re.escape(m) for m in SIDECAR_MODULES) + ")$")

    # Small on purpose: everything here has to exist in whatever interpreter the
    # sandbox's shebang resolves to.
    STDLIB_ALLOWED = frozenset({"re", "json", "os", "sys", "time", "shlex", "itertools", "typing"})

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._sidecar = bool(self._SIDECAR.search(filepath))

    def _check_import(self, node: ast.AST, module: str | None) -> None:
        if not self._sidecar or module is None:
            return
        root = module.split(".")[0]
        if root in self.STDLIB_ALLOWED:
            return
        if root == "__future__":
            # Pointing this at STDLIB_ALLOWED would invite the one edit that
            # silently retires the rule's own guard on future-import syntax.
            self.violation(
                node,
                f"'{module}' is imported by a module copied beside generated sandbox shims. It is not "
                "an import hazard, but the sidecar's import surface is kept minimal and auditable by "
                "an allowlist -- drop the line rather than adding '__future__' to STDLIB_ALLOWED.",
            )
            return
        self.violation(
            node,
            f"'{module}' is imported by a module copied beside generated sandbox shims, which run "
            "where coder_eval and its dependencies are not installed. Use the standard library, or "
            f"add '{root}' to CE057's STDLIB_ALLOWED if it really is stdlib.",
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # A relative import (level > 0) is a package import by definition.
        self._check_import(node, node.module if node.level == 0 else f".{node.module or ''}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(node, alias.name)
        self.generic_visit(node)
