"""CE043: Agents must not truncate a command's output when recording it.

``CommandTelemetry.result_summary`` is contractually the *untruncated* tool-result
body, and its length drives the ``result_tokens`` computed field (the cost
simulator's cache-independent measure of tool-output size). An agent that clips a
command's captured output before storing it silently under-reports every tool
result — exactly the bug the Codex agent shipped with (``f"Output: {output[:100]}"``),
which pinned ~77% of its Bash results at ~31 tokens and skewed the cost model.

This rule flags, inside ``src/coder_eval/agents/``, a constant-upper-bound slice
(``x[:N]``) applied to a value that denotes captured command output:

  * a name whose id is/ends with ``output`` / ``stdout`` / ``stderr``
    (e.g. ``output[:100]``, ``aggregated_output[:512]``, ``proc_stdout[:80]``)
  * an attribute access ``.aggregated_output`` / ``.output`` / ``.stdout`` / ``.stderr``
    (e.g. ``command_item.aggregated_output[:100]``)

Store the output whole (it is already bounded by the harness's own exec-output
truncation) and trim for DISPLAY in the renderers/reports instead.

Add ``# noqa: CE043`` on the offending line for a genuinely non-recorded use
(e.g. slicing stdout only to build a short crash/log message that never becomes a
``result_summary``), with a comment explaining why.
"""

import ast

from tests.lint.rules.base import BaseRule


_OUTPUT_NAMES = {"output", "stdout", "stderr", "aggregated_output"}


def _denotes_output(value: ast.AST) -> str | None:
    """Return the output-ish identifier being sliced, or None."""
    if isinstance(value, ast.Name):
        low = value.id.lower()
        if low in _OUTPUT_NAMES or low.endswith("_output") or low.endswith("_stdout") or low.endswith("_stderr"):
            return value.id
    elif isinstance(value, ast.Attribute):
        if value.attr.lower() in _OUTPUT_NAMES:
            return value.attr
    return None


def _is_const_upper_slice(sl: ast.AST) -> bool:
    """True for ``[:N]`` / ``[:N:...]`` with a constant int upper bound."""
    return (
        isinstance(sl, ast.Slice)
        and sl.lower is None
        and isinstance(sl.upper, ast.Constant)
        and isinstance(sl.upper.value, int)
    )


class NoCommandOutputTruncation(BaseRule):
    id = "CE043"

    def check(self, tree: ast.AST) -> list:
        # Scope to the agent implementations — only they record CommandTelemetry.
        if "/agents/" not in self.filepath.replace("\\", "/"):
            return []
        self.visit(tree)
        return self.violations

    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = _denotes_output(node.value)
        if name is not None and _is_const_upper_slice(node.slice):
            self.violation(
                node,
                f"Do not truncate command output ({name}[:...]); CommandTelemetry.result_summary "
                "must stay whole (it drives result_tokens). Store the full output and trim for "
                "display in the renderers/reports instead.",
            )
        self.generic_visit(node)
