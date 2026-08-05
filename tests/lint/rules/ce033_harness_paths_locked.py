"""CE033: the docker user/permission isolation barrier must not be bypassed.

Scoped to ``isolation/docker_runner.py`` + ``cli/run_task_internal_command.py``
(the two files that stage + lock the docker harness). It flags three ways a
future refactor could silently re-open a leak surface:

(a) A bare ``os.chown`` / ``os.chmod`` / ``os.lchown`` / ``os.fchown`` /
    ``shutil.chown``, OR a ``.chmod()`` / ``.chown()`` / ``.lchown()`` method call on
    any receiver (``Path(x).chmod(...)`` — the idiomatic pathlib bypass) — all
    chmod/chown of harness paths must route through
    ``container_perms.lock_harness_root_0700`` / ``grant_agent_ownership`` (the single
    choke point CE033 protects). ``container_perms.py`` itself is the choke point and
    is out of scope.

(b) A container-level ``"--user"`` flag appended to the docker argv — the agent
    uid drop is per-turn (setpriv / ``options.user``), NOT ``docker run --user``,
    which would run the whole container (grading included) unprivileged.

(c) A raw ``.model_dump(`` used to build the agent-readable ``task.yaml`` — the
    staged task.yaml must go through ``TaskDefinition.agent_safe_dump()`` so
    criteria/reference are stripped. (Flagged as ``model_dump`` fed to a
    ``task.yaml`` / ``safe_dump`` write; the full-criteria root-only sibling
    legitimately uses ``model_dump`` and is exempt via ``# noqa: CE033``.)

Add ``# noqa: CE033`` to a line that is a verified-safe exception (e.g. the
root-only ``task_full.json`` dump).
"""

import ast

from tests.lint.rules.base import BaseRule


_SCOPED_FILES = ("docker_runner.py", "run_task_internal_command.py")

# os.* / shutil.* choke-point bypasses (including the fd/link variants).
_MODULE_CHMOD_CHOWN = {"chown", "chmod", "lchown", "fchown"}
# Any receiver: `Path(x).chmod(...)` / `p.chown(...)` is the idiomatic bypass — the
# whole point of the rule is that harness chmod/chown routes through container_perms,
# so a method call named chmod/chown/lchown on ANY object is flagged in scope.
_METHOD_CHMOD_CHOWN = {"chmod", "chown", "lchown"}


def _attr_call_name(func: ast.expr) -> tuple[str | None, str | None]:
    """Return (module, attr) for ``module.attr(...)`` calls, else (None, None)."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None, None


def _receiver_mentions_task(func: ast.expr) -> bool:
    """True when the attribute chain a call is made on mentions ``task`` — i.e. the
    ``model_dump`` is being taken off a ``TaskDefinition`` (``self.rt.task.model_dump``,
    ``task.model_dump``). Walks the ``.value`` chain collecting Name ids + attr names."""
    node: ast.expr | None = func
    while isinstance(node, ast.Attribute):
        if node.attr == "task":
            return True
        node = node.value
    return isinstance(node, ast.Name) and node.id == "task"


class HarnessPathsLocked(BaseRule):
    id = "CE033"

    def _in_scope(self) -> bool:
        return self.filepath.endswith(_SCOPED_FILES)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope():
            module, attr = _attr_call_name(node.func)
            # (a) bare os.chown/os.chmod/os.lchown/os.fchown/shutil.chown outside the
            # choke point.
            if module in {"os", "shutil"} and attr in _MODULE_CHMOD_CHOWN:
                self.violation(
                    node,
                    f"bare {module}.{attr}() bypasses the isolation barrier choke point; route harness "
                    "chmod/chown through container_perms.lock_harness_root_0700 / grant_agent_ownership.",
                )
            # (a') a chmod/chown/lchown METHOD call on any receiver — `Path(x).chmod(...)`
            # / `p.chown(...)` is the idiomatic pathlib bypass of the choke point. Guard
            # against double-flagging the os.*/shutil.* forms already caught above.
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _METHOD_CHMOD_CHOWN:
                self.violation(
                    node,
                    f".{node.func.attr}() on a path bypasses the isolation barrier choke point; route "
                    "harness chmod/chown through container_perms.lock_harness_root_0700 / grant_agent_ownership.",
                )
            # (c) a raw ``task.model_dump(...)`` (NOT model_dump_json) — the agent-readable
            # staged task.yaml must go through ``TaskDefinition.agent_safe_dump()`` so
            # criteria/reference are stripped. The root-only ``task_full.json`` dump
            # legitimately serializes the full task and is exempt via a CE033 noqa marker.
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_dump"
                and _receiver_mentions_task(node.func)
            ):
                self.violation(
                    node,
                    "raw task.model_dump() may serialize grading material into the agent-readable "
                    "task.yaml; use TaskDefinition.agent_safe_dump() (criteria/reference stripped). "
                    "The root-only task_full.json dump is exempt via `# noqa: CE033`.",
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._in_scope() and isinstance(node.value, str) and node.value == "--user":
            self.violation(
                node,
                'a container-level "--user" flag runs the WHOLE container (grading included) '
                "unprivileged; the agent uid drop is per-turn (setpriv/options.user), not docker run --user.",
            )
        self.generic_visit(node)
