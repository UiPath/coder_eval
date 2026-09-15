"""CE051: a sandbox driver may not be rewritten silently.

In ``src/coder_eval/``, fires on any construction that carries an existing
sandbox config forward while replacing ``driver``:

  * ``SandboxConfig.model_validate({**cfg.model_dump(), "driver": ...})`` (a
    spread dict only; a dict built from scratch is an ordinary construction)
  * ``cfg.model_copy(update={"driver": ...})``
  * ``setattr(cfg, "driver", ...)`` / ``cfg.driver = ...``

A driver downgrade must be an explicit, logged, operator-visible decision.
Exempt: ``models/sandbox.py`` (the model's own construction). Elsewhere,
``# noqa: CE051`` must name the reason, for example an opt-in that refuses by
default and stamps the row.

Rationale: .claude/notes/lint-rules.md § CE051
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_MESSAGE = (
    "This rewrites `driver` on an existing sandbox config. The driver is the isolation "
    "boundary: silently moving a docker task onto the host makes its criteria address paths "
    "and a toolchain that are not there, so they score 0.0 and the row is written back FAILURE "
    "for a run that passed — and its shell runs unsandboxed on this machine. Refuse, or make it "
    "an explicit opt-in that stamps the row, and add `# noqa: CE051` naming the reason."
)


def _has_driver_key(node: ast.expr) -> bool:
    """True when ``node`` is a dict/dict-display whose keys include ``"driver"``."""
    if not isinstance(node, ast.Dict):
        return False
    return any(isinstance(k, ast.Constant) and k.value == "driver" for k in node.keys if k is not None)


class NoDriverOverride(BaseRule):
    id = "CE051"

    # The model's own module legitimately constructs and defaults the field.
    _EXEMPT_PATH = re.compile(r"[/\\]models[/\\]sandbox\.py$")

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(
            re.search(r"(?:^|[/\\])src[/\\]coder_eval[/\\]", filepath)
        ) and not self._EXEMPT_PATH.search(filepath)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope:
            self._check_call(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._in_scope:
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "driver":
                    self.violation(node, _MESSAGE)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call) -> None:
        # cfg.model_copy(update={"driver": ...})
        if isinstance(node.func, ast.Attribute) and node.func.attr == "model_copy":
            for kw in node.keywords:
                if kw.arg == "update" and _has_driver_key(kw.value):
                    self.violation(node, _MESSAGE)
            return
        # SandboxConfig.model_validate({**cfg.model_dump(), "driver": ...})
        if isinstance(node.func, ast.Attribute) and node.func.attr == "model_validate" and node.args:
            arg = node.args[0]
            # Only a SPREAD dict — a literal built from scratch is an ordinary
            # construction, not a rewrite of somebody else's config.
            if _has_driver_key(arg) and isinstance(arg, ast.Dict) and any(k is None for k in arg.keys):
                self.violation(node, _MESSAGE)
            return
        # setattr(cfg, "driver", ...)
        if isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(node.args) >= 2:
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value == "driver":
                self.violation(node, _MESSAGE)
