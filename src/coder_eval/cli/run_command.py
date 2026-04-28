"""Run command - execute evaluation tasks."""

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import typer
from claude_agent_sdk import SdkPluginConfig
from tqdm import tqdm

from ..config import settings
from ..logging_config import setup_logging
from ..models import RunSummary
from ..orchestration.config import BatchRunConfig
from ..path_utils import create_latest_symlink, format_task_log_id
from ..streaming.renderers import RichStreamRenderer
from .console import console
from .run_helpers import (
    discover_default_tasks,
    expand_task_files,
    prepare_run_directory,
    print_execution_mode,
    print_execution_summary,
)


def _resolve_experiment_path(experiment: Path | None) -> Path | None:
    """Resolve an experiment path, supporting bare names like 'model-comparison'.

    Resolution order:
      1. None → None (use default experiment)
      2. Path exists as-is → use it
      3. experiments/{name}.yaml exists → use it
      4. experiments/{name} exists → use it
      5. Raise typer.BadParameter with available experiments
    """
    if experiment is None:
        return None
    if experiment.exists():
        return experiment

    # Try resolving bare name under experiments/ (project-root-relative, not CWD-relative).
    # Path: cli/run_command.py → cli/ → coder_eval/ → src/ → project_root (4 levels).
    # NOTE: This assumes a source checkout. If installed into site-packages, this won't resolve.
    # That's acceptable since experiments/ lives in the repo, not the installed package.
    _project_root = Path(__file__).resolve().parent.parent.parent.parent
    experiments_dir = _project_root / "experiments"
    for candidate in [
        experiments_dir / f"{experiment}.yaml",
        experiments_dir / f"{experiment}.yml",
        experiments_dir / str(experiment),
    ]:
        if candidate.exists():
            return candidate

    # Build helpful error message listing available experiments
    available: list[str] = []
    if experiments_dir.is_dir():
        available = sorted(p.stem for p in experiments_dir.glob("*.yaml") if p.stem != "default")
    hint = f" Available: {', '.join(available)}" if available else ""
    raise typer.BadParameter(f"Experiment not found: {experiment}.{hint}")


def run_command(
    task_files: list[Path] | None = typer.Argument(  # noqa: B008
        None,
        help="Path(s) to task YAML file(s). Defaults to all tasks/ recursively.",
    ),
    preserve: bool = typer.Option(
        True,
        "--preserve/--no-preserve",
        "-p/-P",
        help="Preserve sandbox after execution (default: preserve)",
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
    snapshot_mode: str | None = typer.Option(
        None,
        "--snapshot-mode",
        help="Override snapshot mode for all tasks (disabled/full/incremental/hybrid)",
    ),
    snapshot_checkpoint_freq: int | None = typer.Option(
        None,
        "--snapshot-checkpoint-freq",
        help="Checkpoint frequency for hybrid mode (default: 5)",
        min=1,
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
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override agent model for all tasks (e.g., claude-sonnet-4-20250514)",
    ),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Override permission mode for all tasks (default/acceptEdits/plan/bypassPermissions)",
    ),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help="Override max agent inner-loop turns per iteration for all tasks",
        min=1,
    ),
    task_timeout: int | None = typer.Option(
        None,
        "--task-timeout",
        help="Override task timeout (seconds) for all tasks. Covers the evaluation loop.",
        min=30,
    ),
    turn_timeout: int | None = typer.Option(
        None,
        "--turn-timeout",
        help="Override turn timeout (seconds) for all tasks. Per agent.communicate() call.",
        min=10,
    ),
    stream: str | None = typer.Option(
        None,
        "--stream",
        "-s",
        click_type=click.Choice(["full", "minimal"], case_sensitive=False),
        help="Stream LLM events to terminal: 'full' or 'minimal' (turn-level only). Disables progress bar.",
    ),
    allowed_tools: str | None = typer.Option(
        None,
        "--allowed-tools",
        help="Override allowed tools for all tasks (comma-separated, e.g., 'Read,Write,Bash')",
    ),
    disallowed_tools: str | None = typer.Option(
        None,
        "--disallowed-tools",
        help="Override disallowed tools for all tasks (comma-separated, e.g., 'TodoWrite,Agent')",
    ),
    plugins: str | None = typer.Option(
        None,
        "--plugins",
        help='Override plugins for all tasks (JSON array, e.g., \'[{"name": "my-plugin", "path": "/path"}]\')',
    ),
    ignore_patterns: str | None = typer.Option(
        None,
        "--ignore-patterns",
        help=(
            "Override agent file-change detection ignore patterns (comma-separated, e.g., '*.log,__pycache__')."
            " Does not affect sandbox/snapshot ignore patterns."
        ),
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        click_type=click.Choice(["direct", "bedrock", "proxy"], case_sensitive=False),
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
        help="For dataset-backed tasks, use only the first N rows. Lets you smoke-test datasets cheaply.",
        min=1,
    ),
    repeats: int | None = typer.Option(
        None,
        "--repeats",
        help="Run each (task, variant) N times. Overrides experiment/variant `repeats:`. Must be >=1.",
        min=1,
    ),
) -> None:
    """Run evaluation tasks (optionally in parallel).

    When no TASK_FILES are provided, all .yaml files under tasks/ are discovered recursively.

    Sandboxes are preserved by default for debugging. Use --no-preserve to clean up.

    Examples:

        coder-eval run

        coder-eval run tasks/hello_date.yaml

        coder-eval run tasks/*.yaml --no-preserve

        coder-eval run tasks/*.yaml --run-dir ./my-custom-run

        coder-eval run tasks/*.yaml --max-parallel 3

        coder-eval run tasks/*.yaml --verbose --log-file debug.log

        coder-eval run tasks/*.yaml --tags smoke

        coder-eval run tasks/*.yaml --tags golden,basic --exclude-tags example
    """
    # Validate permission mode early for clear error message
    if permission_mode is not None:
        allowed_modes = {"default", "acceptEdits", "plan", "bypassPermissions"}
        if permission_mode not in allowed_modes:
            raise typer.BadParameter(
                f"Invalid --permission-mode '{permission_mode}'. Choose from: {', '.join(sorted(allowed_modes))}."
            )

    # Parse tag filters
    include_tags = {t.strip() for t in tags.split(",") if t.strip()} if tags else None
    exclude_tags_set = {t.strip() for t in exclude_tags.split(",") if t.strip()} if exclude_tags else None

    # Parse comma-separated list options
    allowed_tools_list = [t.strip() for t in allowed_tools.split(",") if t.strip()] if allowed_tools else None
    disallowed_tools_list = [t.strip() for t in disallowed_tools.split(",") if t.strip()] if disallowed_tools else None
    ignore_patterns_list = [p.strip() for p in ignore_patterns.split(",") if p.strip()] if ignore_patterns else None

    # Parse plugins JSON and validate against SdkPluginConfig schema
    plugins_list: list[SdkPluginConfig] | None = None
    if plugins is not None:
        import json

        from pydantic import TypeAdapter, ValidationError

        try:
            raw = json.loads(plugins)
        except json.JSONDecodeError as e:
            raise typer.BadParameter(f"--plugins must be valid JSON: {e}") from e

        try:
            plugins_list = TypeAdapter(list[SdkPluginConfig]).validate_python(raw)
        except ValidationError as e:
            raise typer.BadParameter(f"--plugins validation failed: {e}") from e

    # Override API backend if --backend was passed
    if backend is not None:
        from coder_eval.models import ApiBackend

        settings.api_backend = ApiBackend(backend)

    # Setup logging before running tasks
    log_level = settings.log_level
    setup_logging(level=log_level, log_file=log_file, verbose=verbose)

    # Default to discovering all tasks under tasks/ when none provided
    resolved_task_files = task_files if task_files else discover_default_tasks()

    # Resolve experiment path: bare names like "model-comparison" → experiments/model-comparison.yaml
    resolved_experiment = _resolve_experiment_path(experiment)

    # Run the async entry point
    try:
        asyncio.run(
            _run_all_tasks(
                resolved_task_files,
                preserve,
                run_dir,
                max_parallel,
                snapshot_mode,
                snapshot_checkpoint_freq,
                include_tags,
                exclude_tags_set,
                model,
                permission_mode,
                max_turns,
                task_timeout,
                turn_timeout,
                stream,
                allowed_tools_list,
                disallowed_tools_list,
                plugins_list,
                ignore_patterns_list,
                experiment_path=resolved_experiment,
                max_rows=sample,
                repeats=repeats,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Execution interrupted.[/yellow]")
        raise typer.Exit(2) from None


async def _run_all_tasks(
    task_files: list[Path],
    preserve: bool,
    run_dir: Path | None,
    max_parallel: int,
    snapshot_mode: str | None,
    snapshot_checkpoint_freq: int | None,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    agent_model: str | None = None,
    permission_mode: str | None = None,
    max_turns: int | None = None,
    task_timeout: int | None = None,
    turn_timeout: int | None = None,
    stream_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    plugins: list[SdkPluginConfig] | None = None,
    ignore_patterns: list[str] | None = None,
    experiment_path: Path | None = None,
    max_rows: int | None = None,
    repeats: int | None = None,
) -> None:
    """Async entry point for running all tasks (optionally in parallel).

    When --experiment is provided (or experiments/default.yaml exists), tasks are
    resolved through the experiment layer and executed via run_batch.
    Otherwise, the legacy run_batch path is used for backward compatibility.

    Args:
        task_files: List of task file paths or glob patterns
        preserve: Whether to preserve sandbox
        run_dir: Custom run directory (or None for auto-generated)
        max_parallel: Maximum number of concurrent tasks
        snapshot_mode: Optional override for snapshot mode
        snapshot_checkpoint_freq: Optional override for checkpoint frequency
        include_tags: Only run tasks matching any of these tags
        exclude_tags: Skip tasks matching any of these tags
        agent_model: Optional override for agent model
        permission_mode: Optional override for permission mode
        max_turns: Optional override for max agent turns
        task_timeout: Optional override for task timeout (seconds)
        turn_timeout: Optional override for turn timeout (seconds)
        stream_mode: Optional stream mode ('full' or 'minimal') for real-time output
        allowed_tools: Optional override for allowed tools
        disallowed_tools: Optional override for disallowed tools
        plugins: Optional override for plugins (SdkPluginConfig objects)
        ignore_patterns: Optional override for agent file change detection ignore patterns
        experiment_path: Optional path to experiment YAML (default: experiments/default.yaml)
    """
    # Prepare run directory
    run_dir = prepare_run_directory(run_dir)

    # Create 'latest' symlink immediately so it's available during the run
    if run_dir.parent == settings.runs_dir:
        create_latest_symlink(settings.runs_dir, run_dir.name)

    # Expand glob patterns and collect task files
    all_task_files = expand_task_files(task_files)

    # Configure batch execution
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=max_parallel,
        preserve_sandbox=preserve,
        snapshot_mode=snapshot_mode,
        snapshot_checkpoint_freq=snapshot_checkpoint_freq,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        agent_model=agent_model,
        permission_mode=permission_mode,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        plugins=plugins,
        ignore_patterns=ignore_patterns,
        task_timeout=task_timeout,
        turn_timeout=turn_timeout,
        max_rows=max_rows,
        repeats=repeats,
    )

    # Always run through experiment layer (defaults to experiments/default.yaml)
    summary, failed_suite_gates = await _run_with_experiment(
        all_task_files, config, experiment_path, stream_mode, max_parallel
    )

    # Aggregate task logs into run.log
    from ..logging_config import aggregate_task_logs

    aggregate_task_logs(run_dir)

    # Print execution summary
    print_execution_summary(run_dir, summary)

    # Exit with non-zero code if any tasks failed, errored, or any suite failed its thresholds.
    if summary.tasks_failed > 0 or summary.tasks_error > 0 or failed_suite_gates > 0:
        raise typer.Exit(1)


async def _run_with_callbacks(
    execute_fn: Callable[..., Any],
    task_count: int,
    stream_mode: str | None,
) -> Any:
    """Run a batch execution function with streaming or progress bar callbacks.

    Handles the shared logic of setting up either a streaming callback factory
    (when --stream is enabled) or a tqdm progress bar (default mode).

    Args:
        execute_fn: Async callable that accepts keyword arguments
            stream_callback_factory, on_task_complete, and on_batch_start.
        task_count: Number of tasks (used for batch_mode detection).
        stream_mode: Optional stream mode ('full' or 'minimal') for real-time output.

    Returns:
        Whatever execute_fn returns.
    """
    if stream_mode:
        batch_mode = task_count > 1
        renderer = RichStreamRenderer(verbosity=stream_mode, batch_mode=batch_mode)
        stream_callback_factory = lambda _task_id, r=renderer: r  # noqa: E731
        return await execute_fn(stream_callback_factory=stream_callback_factory)

    progress_bar: tqdm[Any] | None = None

    def _on_batch_start(count: int) -> None:
        nonlocal progress_bar
        progress_bar = tqdm(total=count, desc="Tasks", unit="task", dynamic_ncols=True, disable=not sys.stderr.isatty())

    def _on_task_complete(result: Any) -> None:
        if progress_bar is None:
            return
        status = result.result.final_status
        label = format_task_log_id(result.variant_id, result.task_id, result.replicate_index)
        status_icon = status.icon
        progress_bar.set_postfix_str(f"{status_icon} {label}")
        progress_bar.update(1)

    try:
        result = await execute_fn(on_task_complete=_on_task_complete, on_batch_start=_on_batch_start)
    finally:
        if progress_bar is not None:
            progress_bar.close()
    return result


async def _run_with_experiment(
    all_task_files: list[Path],
    config: BatchRunConfig,
    experiment_path: Path | None,
    stream_mode: str | None,
    max_parallel: int,
) -> tuple[RunSummary, int]:
    """Run tasks through the experiment resolution layer.

    Loads experiments, resolves task configs (all 5 layers), executes via
    run_batch, and generates experiment reports.

    Args:
        all_task_files: Expanded list of task file paths.
        config: Batch execution configuration.
        experiment_path: Explicit experiment path or None for default.
        stream_mode: Optional stream mode for real-time output.
        max_parallel: Maximum parallel tasks (for batch_mode detection).

    Returns:
        RunSummary with aggregated results.
    """
    from ..orchestration.batch import run_batch
    from ..orchestration.experiment import (
        DEFAULT_EXPERIMENT_PATH,
        aggregate_results,
        load_experiment,
        resolve_all_tasks,
    )  # resolve_task_for_variant not needed here
    from ..reports_experiment import ExperimentReportGenerator

    # Load experiments (avoid double-loading when using default)
    exp_path = experiment_path or DEFAULT_EXPERIMENT_PATH
    try:
        experiment = load_experiment(exp_path)
    except (FileNotFoundError, ValueError) as e:
        raise typer.BadParameter(f"Failed to load experiment '{exp_path}': {e}") from e
    if exp_path == DEFAULT_EXPERIMENT_PATH:
        default_experiment = experiment
    elif DEFAULT_EXPERIMENT_PATH.exists():
        try:
            default_experiment = load_experiment(DEFAULT_EXPERIMENT_PATH)
        except (FileNotFoundError, ValueError) as e:
            raise typer.BadParameter(f"Failed to load default experiment '{DEFAULT_EXPERIMENT_PATH}': {e}") from e
    else:
        default_experiment = experiment  # fall back to custom as its own baseline

    # Resolve tasks through experiment layer (applies all 5 config layers)
    resolved = resolve_all_tasks(
        task_files=all_task_files,
        experiment=experiment,
        default_experiment=default_experiment,
        config=config,
        experiment_file=exp_path,
    )

    # Print execution mode
    print_execution_mode(len(resolved), max_parallel)

    summary, task_results = await _run_with_callbacks(
        execute_fn=lambda **kwargs: run_batch(resolved_tasks=resolved, config=config, **kwargs),
        task_count=len(resolved),
        stream_mode=stream_mode,
    )

    # Generate experiment reports
    experiment_result = aggregate_results(
        experiment_id=experiment.experiment_id,
        description=experiment.description,
        variant_ids=[v.variant_id for v in experiment.variants],
        task_results=task_results,
        total_duration=summary.total_duration_seconds,
    )
    # Reports are written at run root level (no experiment_id subfolder)
    ExperimentReportGenerator.write_reports(experiment_result, config.run_dir, experiment=experiment)

    # Per-suite pass-rate rollups for dataset-backed tasks (no-op when none were used).
    # Pass `resolved` through so suite_thresholds on each criterion can be evaluated.
    from ..reports import write_suite_rollups

    rollups = write_suite_rollups(config.run_dir, task_results, resolved_tasks=resolved)
    failed_gates = [r for r in rollups if not r.passed]
    if failed_gates:
        logging.getLogger(__name__).warning(
            "%d suite gate(s) failed thresholds: %s",
            len(failed_gates),
            ", ".join(f"{r.variant_id}/{r.suite_id}" for r in failed_gates),
        )

    return summary, len(failed_gates)
