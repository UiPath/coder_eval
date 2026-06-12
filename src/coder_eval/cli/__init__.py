"""Command-line interface for coder_eval."""

import typer

from .console import console
from .evaluate_command import evaluate_command
from .plan_command import plan_command
from .proxy_command import proxy_command
from .report_command import report_command
from .run_command import run_command
from .run_task_internal_command import run_task_internal_command


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
    - evaluate: Run criteria against a directory without an agent
    - report: Display or export evaluation reports
    - proxy: Start a local LLM Gateway proxy for Claude Code CLI
    """
    # Discover and register agents (built-in + third-party plugins) before any
    # subcommand resolves a task or builds an agent.
    from coder_eval.plugins import load_plugins

    load_plugins()

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
# Hidden internal command invoked inside the Docker container only.
app.command(name="_run-task-internal", hidden=True)(run_task_internal_command)


__all__ = ["app"]
