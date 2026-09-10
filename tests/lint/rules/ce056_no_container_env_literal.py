"""CE056: no bare ``CODER_EVAL_IN_CONTAINER`` literal outside ``container_paths``.

``models/container_paths.py`` defines ``IN_CONTAINER_ENV`` and its comment states
why: the string is the predicate for four separate gates, and "two half-copies of
the same string in different packages is how a rename becomes a silent no-op".

The constant shipped with that rationale, every READER was migrated to it -- and
the single WRITER was not. ``docker_runner`` kept emitting
``--env CODER_EVAL_IN_CONTAINER=1``, which is the one site that produces the
value all four gates consume. Changing the constant would therefore have updated
every consumer and left the container exporting the old name, so all four gates
would read "not in a container" at once:

  * ``Sandbox.enforces_permission_windows`` -- the reference-solution anti-cheat
    window silently stops being applied, and a run that is NOT protected scores
    like one that is;
  * ``resolve_reference_dir`` -- the ``/work/references`` branch is skipped;
  * ``_should_grade_in_container`` -- a grading container dispatches another
    grading container;
  * the orphan-container heartbeat watchdog's ``os._exit(137)`` gate.

None of those fail loudly. This is the CE053 shape exactly (a rename-safety
constant that shipped beside the literals it was meant to replace), and CE052
cannot catch it -- that rule inspects ``if`` guards, so it never looks at the
writer at all.

Fires on any string constant in ``src/coder_eval/`` (outside the defining module)
that equals the env-var name or embeds it as an ``NAME=value`` assignment.
Import ``IN_CONTAINER_ENV`` from ``coder_eval.models`` instead; ``# noqa: CE056``
for a genuinely unrelated string.
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
