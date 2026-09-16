"""CE068: the kernel names no concrete agent kind.

``orchestration/``, ``streaming/`` and ``timing.py`` are the kernel every agent,
in-tree or plugin, runs through. A branch keyed on one kind there is a behaviour a
plugin kind cannot get. The motivating defect: ``orchestration/overrides.py``
accepted ``-D agent.sdk_options.*`` only when the type was ``AgentKind.CLAUDE_CODE``,
so the out-of-tree Delegate agent, whose config also declares ``sdk_options``,
needed a monkeypatch (``coder_eval_uipath/_overrides_patch.py``). The guard now
asks the registry whether the config class declares the field.

Fires on, inside the kernel:

  * a ``from ... import`` of a concrete agent config class. The set is derived
    from the ``AgentConfig`` union at rule-import time, never listed, so a new
    in-tree kind is covered on arrival;
  * an ``AgentKind.<MEMBER>`` attribute read, except ``AgentKind.UNKNOWN`` (the
    batch sentinel for a task that failed to load, which names no harness).

Blind spots: a kind reached through ``getattr`` or a string literal
(``type == "codex"``) is invisible here, and ``AgentKind`` used as a bare name
(``isinstance(x, AgentKind)``) is allowed. The string-dispatch half is
``no_type_name_string_dispatch``'s job.
"""

import ast
import typing

from coder_eval.models import AgentConfig
from tests.lint.rules._layers import is_kernel_path
from tests.lint.rules.base import BaseRule


def _config_class_names() -> frozenset[str]:
    union = typing.get_args(AgentConfig.__value__)[0]
    return frozenset(cls.__name__ for cls in typing.get_args(union))


CONFIG_CLASS_NAMES = _config_class_names()
_ALLOWED_MEMBERS = frozenset({"UNKNOWN"})
_FIX = "ask the agent registry (a config class's fields or an agent's HarnessContract) instead"


class NoKindNamesInKernel(BaseRule):
    id = "CE068"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_kernel = is_kernel_path(filepath)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._in_kernel:
            for alias in node.names:
                if alias.name in CONFIG_CLASS_NAMES:
                    self.violation(
                        node,
                        f"architectural violation: concrete agent config '{alias.name}' imported into the "
                        f"agent-agnostic kernel — {_FIX}",
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self._in_kernel
            and isinstance(node.value, ast.Name)
            and node.value.id == "AgentKind"
            and node.attr not in _ALLOWED_MEMBERS
        ):
            self.violation(
                node,
                f"architectural violation: 'AgentKind.{node.attr}' named in the agent-agnostic kernel — {_FIX}",
            )
        self.generic_visit(node)
