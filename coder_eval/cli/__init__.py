"""Command-line interface for coder_eval."""

import typer

from .console import console
from .plan_command import plan_command
from .report_command import report_command
from .run_command import run_command


# Create the Typer app
app = typer.Typer(
    name="coder-eval",
    help="A framework for evaluating AI coding agents",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """A framework for evaluating AI coding agents.

    Run 'coder-eval COMMAND --help' for help on a specific command.

    Available commands:
    - run: Execute evaluation tasks
    - plan: Validate task files (dry-run)
    - report: Display or export evaluation reports
    """
    # If no subcommand was invoked, show help and exit
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


# Register commands
app.command(name="run")(run_command)
app.command(name="plan")(plan_command)
app.command(name="report")(report_command)


__all__ = ["app"]
