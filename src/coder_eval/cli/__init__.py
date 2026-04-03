"""Command-line interface for coder_eval."""

import typer

from coder_eval.tools.autogen.cli import autogen_command

from .console import console
from .evaluate_command import evaluate_command
from .plan_command import plan_command
from .proxy_command import proxy_command
from .report_command import report_command
from .run_command import run_command


# Create the Typer app
app = typer.Typer(
    name="coder-eval",
    help="A framework for evaluating AI coding agents",
    add_completion=False,
)

# Tools sub-app (optional authoring utilities, not part of the core eval loop)
tools_app = typer.Typer(
    name="tools",
    help="Optional authoring and utility tools (task generation, etc.)",
    add_completion=False,
)
app.add_typer(tools_app, name="tools")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """A framework for evaluating AI coding agents.

    Run 'coder-eval COMMAND --help' for help on a specific command.

    Available commands:
    - run: Execute evaluation tasks
    - plan: Validate task files (dry-run)
    - evaluate: Run criteria against a directory without an agent
    - report: Display or export evaluation reports
    - proxy: Start a local LLM Gateway proxy for Claude Code CLI
    - tools: Optional authoring utilities (e.g. tools autogen)
    """
    # If no subcommand was invoked, show help and exit
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


# Register core commands
app.command(name="run")(run_command)
app.command(name="plan")(plan_command)
app.command(name="evaluate")(evaluate_command)
app.command(name="report")(report_command)
app.command(name="proxy")(proxy_command)

# Register tools subcommands
tools_app.command(name="autogen")(autogen_command)


__all__ = ["app"]
