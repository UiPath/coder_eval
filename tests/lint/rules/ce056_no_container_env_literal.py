"""CE056: no bare ``CODER_EVAL_IN_CONTAINER`` literal outside ``container_paths``.

Fires on any string constant in ``src/coder_eval/``, outside
``models/container_paths.py``, that equals the env-var name or starts with ``NAME=``
(the child-process assignment form). Prose that only mentions the name does not fire.
Import ``IN_CONTAINER_ENV`` from ``coder_eval.models`` instead; add ``# noqa: CE056``
for a genuinely unrelated string.

HAZARD: several gates read this one variable (see ``models/container_paths.py``). A
WRITER left on the literal disarms all of them silently after a rename. CE052 does not
cover this: it inspects ``if`` guards, never the writer.

Rationale: .claude/notes/lint-rules.md § CE056
"""

import ast
import re

from tests.lint.rules.base import BaseRule


def _container_env_name() -> str:
    """Read from the module rather than retyped -- retyping it here would make
    this rule the third copy of the string it exists to forbid."""
    from coder_eval.models import IN_CONTAINER_ENV

    return IN_CONTAINER_ENV


class NoContainerEnvLiteral(BaseRule):
    id = "CE056"

    # `(^|sep)` so a repo-relative path is in scope too; see CE054.
    _SRC_PATH = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
    # The module that DEFINES it.
    _EXEMPT = re.compile(r"[/\\]container_paths\.py$")
    _name: str | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._SRC_PATH.search(filepath)) and not self._EXEMPT.search(filepath)
        if self._in_scope and NoContainerEnvLiteral._name is None:
            NoContainerEnvLiteral._name = _container_env_name()

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._in_scope and isinstance(node.value, str):
            self._check(node, node.value)
        self.generic_visit(node)

    def _check(self, node: ast.Constant, value: str) -> None:
        name = NoContainerEnvLiteral._name
        if name is None:
            return
        # Exact, or the `NAME=value` child-process form. Not a bare `in`: prose
        # in a docstring or an error message NAMES the variable on purpose, and
        # this rule must not push authors to obfuscate their own explanations.
        if value == name or value.startswith(f"{name}="):
            self.violation(
                node,
                f"{value!r} names the in-container gate by literal. `IN_CONTAINER_ENV` exists in "
                "coder_eval.models precisely so a rename cannot leave half the tree behind -- and "
                "it shipped while the one site that SETS the variable kept the string, which would "
                "have disarmed the reference anti-cheat window, the reference mount, the grading- "
                "container recursion guard and the watchdog together, all silently. Import "
                "IN_CONTAINER_ENV, or add `# noqa: CE056` if the string is unrelated.",
            )
