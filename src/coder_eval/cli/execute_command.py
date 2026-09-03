"""Execute command - run evaluation tasks WITHOUT grading them.

``coder-eval execute`` is ``coder-eval run`` with the grading half removed: the
sandbox is built, the agent runs, and the full trajectory is captured into the
usual ``task.json`` / ``run.json`` layout — but no success criterion is checked,
``weighted_score`` stays ``None``, and each row finalizes as
``FinalStatus.NOT_GRADED``.

It exists so an *external* harness can own the verdict. The motivating case is
Harbor (Terminal-Bench 2.0), which builds its own container, calls coder-eval as
the agent, and grades with its own ``tests/test.sh``. Grading twice there would
be worse than not grading at all: coder-eval's verdict would be reported
alongside Harbor's without being the one that counts.

Every flag on ``run`` is available here except two, and both omissions are
deliberate:

* ``--junit-xml`` — a JUnit report is a report of verdicts, and there are none.
* ``--resume`` — ``partition_for_resume`` treats "has any final status" as
  finalized, so a ``NOT_GRADED`` row would be skipped by a later ``run --resume``
  rather than graded. Supporting it needs resume to distinguish "done" from
  "executed but unscored"; until then, refusing is the honest option.

The command shares ``run``'s entire body (``run_command.run_pipeline``); only the
Typer signature is restated, because Typer builds its parser from the signature.
``tests/test_execute_command.py`` asserts the two signatures stay in step.
"""

from pathlib import Path

import click
import typer

from ..models import PreservationMode
from .run_command import run_pipeline


def execute_command(
    task_files: list[Path] | None = typer.Argument(  # noqa: B008
        None,
        help="Path(s) to task YAML file(s). Defaults to all tasks/ recursively.",
    ),
    preservation_mode: PreservationMode | None = typer.Option(  # noqa: B008
        None,
        "--preservation-mode",
        help=(
            "How to persist each task's sandbox: NONE (delete), MOVE_ON_WRITE "
            "(run in a tempdir, move into run_dir/artifacts), or DIRECT_WRITE "
            "(run directly in run_dir/artifacts). Default is driver-derived — "
            "docker → DIRECT_WRITE, else MOVE_ON_WRITE. Explicit value always wins."
        ),
    ),
    run_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--run-dir",
        help="Custom run directory (default: auto-generated timestamped directory in runs/)",
    ),
    max_parallel: int = typer.Option(
        1,
        "--max-parallel",
        "-j",
        help="Maximum number of tasks to run concurrently (default: 1 = sequential)",
        min=1,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose (DEBUG level) logging",
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--log-file",
        help="Log to file in addition to console",
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        "-t",
        help="Only run tasks matching any of these tags (comma-separated, e.g., 'smoke,golden')",
    ),
    exclude_tags: str | None = typer.Option(
        None,
        "--exclude-tags",
        help="Skip tasks matching any of these tags (comma-separated, e.g., 'example,integration')",
    ),
    include_skipped: bool = typer.Option(
        False,
        "--include-skipped",
        help=(
            "Also run tasks marked `skip: true` in their YAML. Off by default so the "
            "nightly/CI keep excluding them; use for on-demand / local runs of "
            "quarantined or opt-in tasks."
        ),
    ),
    agent_type: str | None = typer.Option(
        None,
        "--type",
        "-T",
        help="Override agent type for all tasks (e.g. 'claude-code', 'codex', or a plugin kind)",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override agent model for all tasks (e.g., claude-sonnet-4-20250514)",
    ),
    stream: str | None = typer.Option(
        None,
        "--stream",
        "-s",
        click_type=click.Choice(["full", "minimal"], case_sensitive=False),
        help="Stream LLM events to terminal: 'full' or 'minimal' (turn-level only). Disables progress bar.",
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        click_type=click.Choice(["direct", "bedrock", "litellm"], case_sensitive=False),
        help="API backend (default: from API_BACKEND env var)",
    ),
    experiment: Path | None = typer.Option(  # noqa: B008
        None,
        "--experiment",
        "-e",
        help="Experiment definition YAML (default: experiments/default.yaml)",
    ),
    sample: int | None = typer.Option(
        None,
        "--sample",
        help=(
            "For dataset-backed tasks, use a random N-row sample "
            "(fixed seed: reproducible, unbiased across paths). Cheap dataset smoke-test."
        ),
        min=1,
    ),
    sample_per_stratum: int | None = typer.Option(
        None,
        "--sample-per-stratum",
        help=(
            "For dataset-backed tasks, keep up to N rows per stratum (stratify_field, "
            "default expected_skill) — a stratified sample that overrides the task's "
            "dataset.sample_per_stratum without editing the YAML. Ignored when --sample is set. "
            "Nondeterministic (re-draws each run) unless the task sets dataset.sample_seed."
        ),
        min=1,
    ),
    repeats: int | None = typer.Option(
        None,
        "--repeats",
        help="Run each (task, variant) N times. Overrides experiment/variant `repeats:`. Must be >=1.",
        min=1,
    ),
    driver: str | None = typer.Option(
        None,
        "--driver",
        click_type=click.Choice(["tempdir", "docker"], case_sensitive=False),
        help="Override sandbox driver for all tasks. 'docker' runs each task in a fresh container.",
    ),
    set_overrides: list[str] = typer.Option(  # noqa: B008
        [],
        "--set",
        "-D",
        metavar="PATH=VALUE",
        help=(
            "Override any resolved task-config field under agent/run_limits/sandbox, "
            "e.g. -D run_limits.max_turns=30 -D agent.permission_mode=plan "
            "-D agent.sdk_options.effort=high -D sandbox.docker.network=none. "
            "Repeatable. Validated against the schema. A path set by both an alias "
            "and -D is an error; values are YAML-parsed (on/off/yes/no stay strings). "
            "(--model and --driver are shorthand aliases for -D agent.model / "
            "-D sandbox.driver.)"
        ),
    ),
) -> None:
    """Run evaluation tasks WITHOUT checking their success criteria.

    Identical to `coder-eval run` except that nothing is graded: each task
    executes, its full trajectory is captured to task.json, and the row
    finalizes as NOT_GRADED with no weighted_score. Use it when an external
    harness owns the verdict, or to separate an expensive agent run from
    grading you want to iterate on afterwards.

    Grade the results afterwards with `coder-eval evaluate`.

    Execution failures still fail: a crash, timeout, or budget breach reports
    ERROR / TIMEOUT / TOKEN_BUDGET_EXCEEDED and exits non-zero exactly as under
    `run`. Only the verdict is withheld, never the facts of the run.

    Not supported here: --junit-xml (no verdicts to report), --resume (a
    NOT_GRADED row would be mistaken for a finalized one), and simulation tasks
    (their turn-continuation logic reads criteria results).

    Examples:

        coder-eval execute tasks/hello_date.yaml

        coder-eval execute tasks/*.yaml --run-dir ./my-run --max-parallel 3
    """
    run_pipeline(
        grade=False,
        task_files=task_files,
        preservation_mode=preservation_mode,
        run_dir=run_dir,
        # Not exposed as flags — see the module docstring for why each is refused.
        resume=False,
        junit_xml=None,
        max_parallel=max_parallel,
        verbose=verbose,
        log_file=log_file,
        tags=tags,
        exclude_tags=exclude_tags,
        include_skipped=include_skipped,
        agent_type=agent_type,
        model=model,
        stream=stream,
        backend=backend,
        experiment=experiment,
        sample=sample,
        sample_per_stratum=sample_per_stratum,
        repeats=repeats,
        driver=driver,
        set_overrides=set_overrides,
    )
