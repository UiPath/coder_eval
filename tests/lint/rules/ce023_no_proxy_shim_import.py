"""CE023: Don't import from the deprecated coder_eval.proxy.* shim in runtime code.

PR #463 removed the LLM Gateway proxy subsystem and relocated pricing to the
top-level coder_eval.pricing module. What remains under coder_eval/proxy/ is a
backwards-compatibility shim: proxy/pricing.py re-exports coder_eval.pricing
behind a DeprecationWarning, kept only so out-of-tree consumers' existing
`import coder_eval.proxy.pricing` keep working. In-tree runtime/agent code must
import the authoritative location directly (coder_eval.pricing) so a new agent
can't silently couple to the relocated/shimmed path — e.g. adding model rates to
a dict the proxy removal deleted, leaving them unpriced after merge.

Both `from coder_eval.proxy... import ...` and bare `import coder_eval.proxy...`
forms are checked. Skipped for files inside coder_eval/proxy/ (the shim itself).
Use `# noqa: CE023` for a deliberate backwards-compatibility reference.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


class NoProxyShimImports(BaseRule):
    id = "CE023"

    _PROXY_IMPORT = re.compile(r"^coder_eval\.proxy\b")
    _SKIP_PATH = re.compile(r"[/\\]coder_eval[/\\]proxy[/\\]")

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._skip_file = bool(self._SKIP_PATH.search(filepath))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._skip_file and node.module and self._PROXY_IMPORT.match(node.module):
            names = ", ".join(a.name for a in node.names)
            self.violation(
                node,
                f"import from deprecated shim '{node.module}'; "
                f"import directly from the authoritative module, e.g. 'from coder_eval.pricing import {names}'",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if not self._skip_file:
            for alias in node.names:
                if self._PROXY_IMPORT.match(alias.name):
                    self.violation(
                        node,
                        f"import of deprecated shim '{alias.name}'; "
                        f"import directly from the authoritative module (e.g. coder_eval.pricing)",
                    )
        self.generic_visit(node)
