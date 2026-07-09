"""CE001: Within-project imports must use the coder_eval.models top-level.

This rule enforces an *internal* convention — external consumers of the
package are not affected. All model types are re-exported from
coder_eval.models.__init__, and importing from submodules
(e.g. `from coder_eval.models.enums import …`, `import coder_eval.models.enums`)
bypasses that contract and couples callers to the internal layout.

Why a custom rule rather than ruff's TID251 (banned-api)? TID251 requires
listing each submodule explicitly and grows stale as new submodules are
added; the regex-based pattern match here automatically covers any future
`coder_eval.models.*` submodule with no config maintenance.

Skipped for:
- Files inside coder_eval/models/ (need internal cross-imports).
- Imports inside `if TYPE_CHECKING:` blocks (type-only, no runtime effect).

Both `from coder_eval.models.X import Y` and bare `import coder_eval.models.X`
forms are checked. Use `# noqa: CE001` to suppress legitimate cases such as
runtime introspection of a submodule's contents.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


class NoSubmoduleModelImports(BaseRule):
    id = "CE001"

    _SUBMODULE_IMPORT = re.compile(r"^coder_eval\.models\.\w")
    _SKIP_PATH = re.compile(r"[/\\]coder_eval[/\\]models[/\\]")

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._skip_file = bool(self._SKIP_PATH.search(filepath))
        self._type_checking_depth = 0

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self._type_checking_depth += 1
            self.generic_visit(node)
            self._type_checking_depth -= 1
        else:
            self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (
            not self._skip_file
            and not self._type_checking_depth
            and node.module
            and self._SUBMODULE_IMPORT.match(node.module)
        ):
            names = ", ".join(a.name for a in node.names)
            self.violation(
                node,
                f"import from submodule '{node.module}'; use 'from coder_eval.models import {names}'",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if not self._skip_file and not self._type_checking_depth:
            for alias in node.names:
                if self._SUBMODULE_IMPORT.match(alias.name):
                    self.violation(
                        node,
                        f"import of submodule '{alias.name}'; import the symbol from 'coder_eval.models' instead",
                    )
        self.generic_visit(node)


def _is_type_checking(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )
