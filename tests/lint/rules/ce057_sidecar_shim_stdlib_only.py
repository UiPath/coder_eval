"""CE057: a sidecar module copied beside a generated sandbox shim stays stdlib-only.

``Sandbox._generate_cli_recorders`` writes every module in
``models.sandbox.SIDECAR_MODULES`` into the recorder directory beside each
``record_cli`` shim that declares response rules, and the shim imports it as a
sibling. That sidecar runs inside the sandbox, where ``coder_eval`` is not
installed and no project dependency is guaranteed, so one
``from coder_eval.models import ...`` or ``import pydantic`` makes every shadowed
CLI die with an ImportError the moment the agent runs it. It surfaces as "the
tool is broken", never as "the harness wrote an unimportable sidecar", and it
costs a whole run to diagnose.

Import-time enforcement (a test that renders and executes a shim) only catches it
when a test happens to declare a response rule; this rule catches it the moment
the import is written.

A stdlib module that is genuinely needed is added to ``STDLIB_ALLOWED`` below --
deliberately an allowlist rather than a check against ``sys.stdlib_module_names``,
so growing the sidecar's surface is a decision someone makes on purpose.

``from __future__ import ...`` falls out of that allowlist too, and is reported
separately: it is not an import hazard (every interpreter that can run the shim
supports it), so the fix is to drop the line rather than widen the allowlist --
which is what the generic message would otherwise suggest.
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
