"""CE002: Blocking I/O inside async def must be wrapped with asyncio.to_thread() or anyio.to_thread.run_sync().

Calling shutil.rmtree(), open(), subprocess.run(), etc. directly from an
async function blocks the event loop and can cause hangs or degraded
concurrency.  Use asyncio.to_thread(fn, *args) or anyio.to_thread.run_sync().

Scope vs ruff's ASYNC2xx:
- Ruff's ASYNC220/230/251 already cover subprocess, open, and time.sleep.
- CE002's value is the gap: shutil/os/pathlib calls and bound methods like
  Path.read_text / Path.write_text that ruff does not flag.

False-positive reduction:
- Calls inside a nested sync `def` or `lambda` are ignored.
- Calls that appear as arguments to asyncio.to_thread / anyio run_sync are suppressed.

Coverage caveat: `_BLOCKING_BUILTINS`, `_BLOCKING_ATTRS`, and `_BLOCKING_METHODS`
are intentionally an explicit allowlist — third-party libs and custom
wrappers will not be caught. Extend these sets when a new blocking pattern
is found in code review.
"""

import ast

from tests.lint.rules.base import BaseRule


_BLOCKING_BUILTINS = {"open"}

_BLOCKING_ATTRS: dict[str, set[str]] = {
    "shutil": {"rmtree", "copytree", "copy", "copy2", "move", "copyfile", "copyfileobj"},
    "os": {"makedirs", "mkdir", "rename", "remove", "unlink", "chmod", "scandir", "listdir", "walk"},
    "subprocess": {"run", "call", "check_output", "check_call", "Popen"},
    "time": {"sleep"},
}

_BLOCKING_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes", "unlink", "rename", "mkdir", "rmdir"}


def _is_to_thread(node: ast.Call) -> bool:
    f = node.func
    if not isinstance(f, ast.Attribute):
        return False
    if f.attr == "to_thread" and isinstance(f.value, ast.Name) and f.value.id == "asyncio":
        return True
    return f.attr == "run_sync"


def _blocking_desc(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name) and f.id in _BLOCKING_BUILTINS:
        return f.id
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.value.id in _BLOCKING_ATTRS and f.attr in _BLOCKING_ATTRS[f.value.id]:
            return f"{f.value.id}.{f.attr}"
        if f.attr in _BLOCKING_METHODS:
            return f"<obj>.{f.attr}"
    return None


class NoBlockingIoInAsync(BaseRule):
    id = "CE002"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._async_depth = 0
        self._to_thread_depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested sync function resets async context.
        saved = self._async_depth
        self._async_depth = 0
        self.generic_visit(node)
        self._async_depth = saved

    def visit_Lambda(self, node: ast.Lambda) -> None:
        saved = self._async_depth
        self._async_depth = 0
        self.generic_visit(node)
        self._async_depth = saved

    def visit_Call(self, node: ast.Call) -> None:
        if _is_to_thread(node):
            self._to_thread_depth += 1
            self.generic_visit(node)
            self._to_thread_depth -= 1
            return

        if self._async_depth > 0 and self._to_thread_depth == 0:
            desc = _blocking_desc(node)
            if desc:
                self.violation(
                    node,
                    (
                        f"blocking call '{desc}' inside async def — "
                        "wrap with asyncio.to_thread() or anyio.to_thread.run_sync()"
                    ),
                )

        self.generic_visit(node)
