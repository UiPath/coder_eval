"""CE047: modules embedded into a generated sandbox shim import stdlib only.

``invocation_log.render_recorder`` embeds the SOURCE of ``coder_eval/argv_match.py``
into every ``record_cli`` shim that declares response rules. That shim runs inside
the sandbox, where ``coder_eval`` is not installed and no project dependency is
guaranteed — so a single ``from coder_eval.models import ...`` or ``import
pydantic`` added to the embedded module makes every shadowed CLI die with an
ImportError the moment the agent runs it. The failure surfaces as "the tool is
broken", never as "the harness embedded an unimportable module", and it costs a
whole run to diagnose.

Import-time enforcement (a test that renders and executes a shim) only catches it
when a test happens to declare a response rule; this rule catches the import the
moment it is written.

A stdlib module that is genuinely needed is added to ``STDLIB_ALLOWED`` below —
deliberately an allowlist rather than a check against ``sys.stdlib_module_names``,
so growing the shim's surface is a decision someone makes on purpose.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


class EmbeddedShimStdlibOnly(BaseRule):
    id = "CE047"

    # Modules whose source is embedded into a generated shim. Keyed by path
    # fragment so the rule fires on the file itself, wherever the tree is rooted.
    _EMBEDDED = re.compile(r"[/\\]coder_eval[/\\]argv_match\.py$")

    # Small on purpose: everything here has to exist in whatever interpreter the
    # sandbox's shebang resolves to.
    STDLIB_ALLOWED = frozenset({"re", "json", "os", "sys", "time", "shlex", "itertools", "typing"})

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._embedded = bool(self._EMBEDDED.search(filepath))

    def _check(self, node: ast.AST, module: str | None) -> None:
        if not self._embedded or module is None:
            return
        root = module.split(".")[0]
        if root in self.STDLIB_ALLOWED:
            return
        self.violation(
            node,
            f"'{module}' is imported by a module embedded into generated sandbox shims, which run "
            "where coder_eval and its dependencies are not installed. Use the standard library, or "
            f"add '{root}' to CE047's STDLIB_ALLOWED if it really is stdlib.",
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # A relative import (level > 0) is a package import by definition.
        self._check(node, node.module if node.level == 0 else f".{node.module or ''}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check(node, alias.name)
        self.generic_visit(node)
