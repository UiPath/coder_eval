"""CE032: Criterion checkers must resolve sandbox paths through the Sandbox seam.

Under `coder_eval/criteria/`, a `/` join onto `<expr>.sandbox_dir` fires. Use
`sandbox.file_exists` / `sandbox.get_file_content` / `sandbox.resolve_files`, the
single place criterion `path` semantics live. Reading `sandbox.sandbox_dir` on its
own (an initialization guard, or passing the root to a sub-agent) is fine.

Use `# noqa: CE032` for a checker that genuinely needs the raw root (e.g. it walks
a directory tree rather than addressing a file).

Rationale: .claude/notes/lint-rules.md § CE032
"""

import ast
import re

from tests.lint.rules.base import BaseRule


class CriteriaPathSeam(BaseRule):
    id = "CE032"

    _CRITERIA_PATH = re.compile(r"[/\\]coder_eval[/\\]criteria[/\\]")

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._CRITERIA_PATH.search(filepath))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if (
            self._in_scope
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "sandbox_dir"
        ):
            message = (
                "criterion checker joins a path onto 'sandbox_dir', bypassing the path seam; use "
                "sandbox.file_exists / sandbox.get_file_content / sandbox.resolve_files so the field "
                "inherits literal-first resolution, glob expansion and ignore filtering"
            )
            self.violation(node, message)
        self.generic_visit(node)
