"""Command-line interface for coder_eval."""

import typer

from coder_eval.telemetry import track_command

from .aggregate_command import aggregate_command
from .console import console
from .evaluate_command import evaluate_command
from .execute_command import execute_command
from .plan_command import plan_command
from .report_command import report_command
from .run_command import run_command
from .run_task_internal_command import run_task_internal_command


# Create the Typer app
app = typer.Typer(
    name="coder-eval",
    help="A framework for evaluating AI coding agents",
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the installed coder-eval version and exit (eager `--version`)."""
    if value:
        from coder_eval import __version__

        console.print(__version__)
        raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed coder-eval version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """A framework for evaluating AI coding agents.

    Run 'coder-eval COMMAND --help' for help on a specific command.

    Available commands:
    - run: Execute evaluation tasks and grade them
    - execute: Execute evaluation tasks WITHOUT grading them
    - plan: Validate task files (dry-run)
    - evaluate: Grade a directory against a task, or re-grade a finished run
    - report: Display or export evaluation reports
    - aggregate: Rebuild run.json/run.md from finalized task.json files
    """
    # Discover and register agents (built-in + third-party plugins) before any
    # subcommand resolves a task or builds an agent.
    from coder_eval.plugins import load_plugins

    load_plugins()

    # One-time telemetry init (no-op when disabled / no connection string).
    # Runs before the no-subcommand early-exit so even `--help` inits harmlessly.
    from coder_eval import __version__
    from coder_eval.telemetry import init_telemetry

    init_telemetry(version=__version__)

    # If no subcommand was invoked, show help and exit
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


# Register core commands. Each public command is wrapped with track_command so it
# emits a CoderEval.Cli.<name> event (Status/DurationMs/ErrorType) on completion;
# functools.wraps preserves the signature so Typer still parses each command's flags.
app.command(name="run")(track_command("run")(run_command))
app.command(name="execute")(track_command("execute")(execute_command))
app.command(name="plan")(track_command("plan")(plan_command))
app.command(name="evaluate")(track_command("evaluate")(evaluate_command))
app.command(name="report")(track_command("report")(report_command))
app.command(name="aggregate")(track_command("aggregate")(aggregate_command))
# Hidden internal command invoked inside the Docker container only — UNWRAPPED
# (it runs inside the run-task subprocess and would double-count / pollute events).
app.command(name="_run-task-internal", hidden=True)(run_task_internal_command)


__all__ = ["app"]
