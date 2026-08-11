"""CE032: Criterion checkers must resolve sandbox paths through the Sandbox seam.

`Sandbox.resolve_files` is the single place criterion `path` semantics live:
literal-first resolution (so a real file named `report[2024].json` is not
reinterpreted as a character class), glob expansion for artifacts whose location
the prompt does not pin, ignore-pattern filtering (so `.venv` / `node_modules` /
`dist` cannot be graded as agent output), and exactly-one enforcement on content
reads. A checker that builds its own path with `sandbox.sandbox_dir / <field>`
and reads it directly silently opts out of all of that, so path semantics differ
per criterion — which is exactly how `reference_comparison.agent_file` drifted
from every other path field.

Use `sandbox.file_exists` / `sandbox.get_file_content` / `sandbox.resolve_files`
instead. Only files under `coder_eval/criteria/` are checked; reading
`sandbox.sandbox_dir` on its own (an initialization guard, or passing the root
to a sub-agent) is fine — the rule fires on joining a path onto it.

Use `# noqa: CE032` for a checker that genuinely needs the raw root (e.g. it
walks a directory tree rather than addressing a file).
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
