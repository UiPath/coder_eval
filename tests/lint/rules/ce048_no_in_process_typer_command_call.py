"""CE048: never call a Typer command function in process.

Typer builds a command's parser from its signature, so every parameter's default
is an ``OptionInfo`` / ``ArgumentInfo`` sentinel, not the value it stands for.
Click substitutes the real defaults when it *invokes* the command; a direct
Python call does not — every unspecified argument arrives as a truthy sentinel
object.

The failure is silent, which is what makes it worth a rule. ``evaluate``'s
``in_place: bool | None = typer.Option(None, "--in-place/--copy")`` reads as "no
preference" and selects copy-vs-in-place from the target shape; called
in-process, ``in_place`` was an ``OptionInfo``, which is truthy, so the tests
silently graded in place and the default they meant to cover was never
exercised. Nothing failed — the wrong branch simply ran.

The fix is the one already applied to ``run`` / ``execute`` / ``evaluate``: keep
the Typer signature as a thin wrapper and put the body in a plain function with
real Python defaults (``run_pipeline``, ``run_evaluation``). Call THAT.

Use ``# noqa: CE048`` only where the sentinel behavior is itself under test.
"""

import ast
import re
from pathlib import Path

from tests.lint.rules.base import BaseRule


_CLI_ROOT = Path("src/coder_eval/cli")


def _typer_command_names() -> set[str]:
    """Functions whose signature is a Typer parser — i.e. whose parameters carry
    ``typer.Option`` / ``typer.Argument`` defaults.

    Detected by the defaults rather than by the ``app.command(...)`` registration
    site, because registration happens in ``cli/__init__.py`` by reference and a
    command that is merely *about* to be registered has the same hazard.
    """
    names: set[str] = set()
    for path in sorted(_CLI_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unparseable file in cli/ fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(_is_typer_param(default) for default in node.args.defaults):
                names.add(node.name)
    return names


def _is_typer_param(node: ast.expr) -> bool:
    """``typer.Option(...)`` / ``typer.Argument(...)`` as a parameter default."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in {"Option", "Argument"}


class NoInProcessTyperCommandCall(BaseRule):
    id = "CE048"

    # The registration site itself hands these to Typer by reference; and the
    # module that defines a command may call its own sibling.
    _EXEMPT_FILES = re.compile(r"[/\\]src[/\\]coder_eval[/\\]cli[/\\]__init__\.py$")
    _commands: set[str] | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = not self._EXEMPT_FILES.search(filepath)
        self._imported_from_cli: set[str] = set()
        if NoInProcessTyperCommandCall._commands is None:
            NoInProcessTyperCommandCall._commands = _typer_command_names()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Only names imported FROM a cli module count. A command name is not
        # unique in the tree — `run_command` is both a Typer command and
        # `Sandbox.run_command` — so matching on the bare name alone would flag
        # every criterion that shells out.
        if (node.module and "coder_eval.cli" in node.module) or (node.level and node.module == "cli"):
            self._imported_from_cli.update(alias.asname or alias.name for alias in node.names)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check(node)
        self.generic_visit(node)

    def _check(self, node: ast.Call) -> None:
        if not self._in_scope or not isinstance(node.func, ast.Name):
            return
        name = node.func.id
        if name not in self._imported_from_cli:
            return
        if name not in (NoInProcessTyperCommandCall._commands or set()):
            return
        self.violation(
            node,
            f"'{name}' is a Typer command: its parameter defaults are OptionInfo sentinels, not values, "
            + "so calling it in process hands every unspecified argument a truthy placeholder and "
            + "silently runs the wrong branch. Call the plain-function body instead (the "
            + "run_pipeline / run_evaluation split exists for this), or drive it through "
            + "typer.testing.CliRunner.",
        )
