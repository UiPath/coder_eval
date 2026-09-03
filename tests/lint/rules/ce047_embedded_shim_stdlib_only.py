"""CE047: a module embedded into a generated sandbox shim stays stdlib-only and namespace-clean.

``invocation_log.render_recorder`` splices the SOURCE of every module in
``invocation_log.EMBEDDED_MODULES`` into each ``record_cli`` shim that declares
response rules. That shim runs inside the sandbox, where ``coder_eval`` is not
installed and no project dependency is guaranteed, and the spliced source shares
one module namespace with the shim's own definitions. Two ways to break it, both
silent:

* **An import.** One ``from coder_eval.models import ...`` or ``import pydantic``
  makes every shadowed CLI die with an ImportError the moment the agent runs it.
  It surfaces as "the tool is broken", never as "the harness embedded an
  unimportable module", and it costs a whole run to diagnose.
* **A name collision.** An embedded module that binds a top-level name the shim
  also binds (``record``, ``main``, ``RULES``, ...) is rebound by the shim's own
  definition further down the file. The resulting TypeError is caught by the
  shim's ``respond()``, so every invocation quietly falls back to the entry
  defaults instead of its canned response.

Import-time enforcement (a test that renders and executes a shim) only catches
either when a test happens to declare a response rule; this rule catches both the
moment they are written.

A stdlib module that is genuinely needed is added to ``STDLIB_ALLOWED`` below --
deliberately an allowlist rather than a check against ``sys.stdlib_module_names``,
so growing the shim's surface is a decision someone makes on purpose.
"""

import ast
import re

from coder_eval.invocation_log import EMBEDDED_MODULES, SHIM_GLOBALS
from tests.lint.rules.base import BaseRule


class EmbeddedShimStdlibOnly(BaseRule):
    id = "CE047"

    # Derived from the writer's own list, so moving the module moves the rule
    # with it. A hardcoded second copy would match nothing after such a move and
    # pass vacuously -- guarding zero files while reading as a guarantee.
    # tests/test_custom_lint.py asserts the pattern matches a file that exists.
    _EMBEDDED = re.compile(r"[/\\]coder_eval[/\\](?:" + "|".join(re.escape(m) for m in EMBEDDED_MODULES) + ")$")

    # Small on purpose: everything here has to exist in whatever interpreter the
    # sandbox's shebang resolves to.
    STDLIB_ALLOWED = frozenset({"re", "json", "os", "sys", "time", "shlex", "itertools", "typing"})

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._embedded = bool(self._EMBEDDED.search(filepath))

    def _check_import(self, node: ast.AST, module: str | None) -> None:
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

    def _check_name(self, node: ast.AST, name: str) -> None:
        if not self._embedded or name not in SHIM_GLOBALS:
            return
        self.violation(
            node,
            f"module-level '{name}' collides with a name the generated shim binds itself "
            "(invocation_log.SHIM_GLOBALS). The shim's own definition wins, and the resulting failure "
            "is swallowed into 'every invocation gets the fallback response'. Rename it.",
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # A relative import (level > 0) is a package import by definition.
        self._check_import(node, node.module if node.level == 0 else f".{node.module or ''}")
        for alias in node.names:
            # `from typing import TypedDict as RULES` binds RULES, so an import
            # is a collision route as much as a def is.
            self._check_name(node, alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(node, alias.name)
            # A plain `import sys` binds the very module the shim imports anyway,
            # so only a renaming import can put something else under the name.
            if alias.asname is not None:
                self._check_name(node, alias.asname)
        self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> None:
        # Top level only: a name bound inside a function is not in the namespace
        # the splice shares with the shim.
        for statement in node.body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self._check_name(statement, statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        self._check_name(statement, target.id)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                self._check_name(statement, statement.target.id)
        self.generic_visit(node)
