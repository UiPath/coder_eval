"""Run command - execute evaluation tasks."""

import asyncio
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import typer
from tqdm import tqdm

from ..config import Settings, settings
from ..logging_config import setup_logging
from ..models import PreservationMode, ResolvedTask, RunSummary, TaskResult
from ..orchestration.config import BatchRunConfig
from ..path_utils import create_latest_symlink, format_task_log_id
from ..streaming.callbacks import CompositeStreamCallback
from ..streaming.renderers import LoggingStreamRenderer, RichStreamRenderer
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


def _litellm_preflight_error(current_settings: Settings) -> str | None:
    """Return an error message if the ``litellm`` backend's external proxy is
    unreachable, else ``None``.

    Only applies when ``api_backend=litellm`` with an explicit ``LITELLM_BASE_URL``
    (the manual proxy / always-on-sidecar path). Without this check a dead proxy
    makes the Claude SDK hang on the endpoint instead of failing fast. Any HTTP
    response (even non-200) counts as reachable — only a connection/timeout error
    is treated as "proxy down".
    """
    from ..models import ApiBackend

    if current_settings.api_backend != ApiBackend.LITELLM or not current_settings.litellm_base_url:
        return None
    base_url = current_settings.litellm_base_url
    # Reject a scheme-less/non-http(s) URL with a clear message instead of letting
    # urlopen raise a bare ValueError ("unknown url type") that escapes as a
    # traceback. Also makes the `# nosec B310` below honest — the scheme is now
    # constrained to http(s), which is exactly what B310 audits.
    if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
        return (
            f"LITELLM_BASE_URL must be an http(s) URL, got {base_url!r}. "
            "Set it to e.g. http://localhost:4000 (or unset LITELLM_BASE_URL and switch backends)."
        )
    url = f"{base_url.rstrip('/')}/health/liveliness"
    try:
        # B310: url is built from the operator-configured LITELLM_BASE_URL, whose
        # scheme is validated to http(s) just above — not untrusted input; this
        # only probes reachability of that proxy endpoint.
        urllib.request.urlopen(url, timeout=5).close()  # nosec B310
    except urllib.error.HTTPError:
        return None  # server responded (up), just not 200 on this path
    except (urllib.error.URLError, OSError) as exc:
        return (
            f"LiteLLM proxy not reachable at {current_settings.litellm_base_url} (tried {url}): {exc}. "
            "Start it (e.g. litellm/start-litellm.sh) or unset LITELLM_BASE_URL."
        )
    return None


def _build_overrides(
    *,
    model: str | None,
    driver: str | None,
    set_overrides: list[str],
) -> dict[str, Any]:
    """Translate the surviving alias flags (``--model`` / ``--driver``) +
    ``-D``/``--set`` entries into one validated override map (dotted path ->
    typed value).

    Alias flags and ``-D`` share the same engine path. A path set by both an
    alias and ``-D`` (or by two ``-D`` entries) is a hard error so they never
    silently last-win against each other. Every resulting path is validated
    against the schema; ``OverrideError`` is wrapped into ``typer.BadParameter``
    at this CLI boundary. All other task-config knobs (permission mode, turn
    limits, timeouts, tools, plugins, SDK options) are expressed via ``-D``.
    """
    from ..orchestration.config_merge import MergeError, validate_paths
    from ..orchestration.overrides import OverrideError, parse_override

    overrides: dict[str, Any] = {}
    sources: dict[str, str] = {}  # path -> originating flag, for collision messages

    def _add_alias(path: str, value: Any, flag: str) -> None:
        overrides[path] = value
        sources[path] = flag

    if model is not None:
        _add_alias("agent.model", model, "--model")
    if driver is not None:
        _add_alias("sandbox.driver", driver, "--driver")

    # -D / --set entries (hard error on collision with an alias or another -D).
    for raw in set_overrides:
        try:
            path, value = parse_override(raw)
        except OverrideError as e:
            raise typer.BadParameter(str(e)) from e
        if path in sources:
            prior = sources[path]
            if prior == "-D":
                raise typer.BadParameter(f"{path!r} set by -D more than once; specify it once")
            raise typer.BadParameter(f"{path!r} set by both {prior} and -D; specify it once")
        overrides[path] = value
        sources[path] = "-D"

    # Validate every path against the resolved-TaskDefinition schema (the same
    # walk the resolver uses). MergeError carries the did-you-mean suggestion.
    try:
        validate_paths(list(overrides))
    except MergeError as e:
        raise typer.BadParameter(str(e)) from e

    return overrides


def run_command(
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
    resume: bool = typer.Option(
        False,
        "--resume",
        help=(
            "Resume an interrupted run: skip tasks already finalized in --run-dir and "
            "run only the rest, folding prior results into run.json. A task counts as "
            "finalized once it has ANY final status — including FAILED/ERROR — so resume "
            "does NOT retry failures (delete a task's task.json to force a re-run). "
            "Requires --run-dir. A config mismatch (model/backend/flags) is warned, not "
            "refused — the resumed tasks keep their original-config results, so the run "
            "mixes configs; use a fresh --run-dir to keep configs separate."
        ),
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
    junit_xml: Path | None = typer.Option(  # noqa: B008
        None,
        "--junit-xml",
        help="Write a JUnit XML report of task results to this path (for CI test-report ingestion).",
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
        # Open string, not a closed click.Choice: the agent registry (incl. plugin
        # kinds discovered at startup) is the source of truth, and it isn't populated
        # at CLI-definition time. An unregistered kind fails at parse_agent_config with
        # a clear "No agent registered for type ...; Registered kinds: [...]" message.
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
    split: str | None = typer.Option(
        None,
        "--split",
        help=(
            "For dataset-backed tasks, keep only rows whose dataset.split_field value "
            "(default field: split) matches this name — e.g. --split train / --split test. "
            "Applied BEFORE --sample / --sample-per-stratum, so a sampled split keeps a "
            "predictable size. Tasks whose rows are all unlabelled are unaffected; a "
            "labelled task with no row in this split aborts the run with an error naming "
            "the splits that exist."
        ),
    ),
    repeats: int | None = typer.Option(
        None,
        "--repeats",
        help="Run each (task, variant) N times. Overrides experiment/variant `repeats:`. Must be >=1.",
        min=1,
    ),
    # typer types this as str|None at signature level; click.Choice narrows
    # the runtime value to {"tempdir","docker"}. BatchRunConfig.driver
    # expects the Literal; the field validator accepts any str and the
    # Choice constraint plus the experiment-layer Literal hint keep us safe.
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
    """Run evaluation tasks (optionally in parallel).

    When no TASK_FILES are provided, all .yaml files under tasks/ are discovered recursively.

    Sandboxes are preserved by default for debugging (driver-derived mode).
    Use --preservation-mode NONE to clean up.

    Examples:

        coder-eval run

        coder-eval run tasks/hello_date.yaml

        coder-eval run tasks/*.yaml --preservation-mode NONE

        coder-eval run tasks/*.yaml --run-dir ./my-custom-run

        coder-eval run tasks/*.yaml --max-parallel 3

        coder-eval run tasks/*.yaml --verbose --log-file debug.log

        coder-eval run tasks/*.yaml --tags smoke

        coder-eval run tasks/*.yaml --tags golden,basic --exclude-tags example
    """
    # --resume needs an explicit run dir to resume into (auto-generated dirs are always fresh).
    if resume and run_dir is None:
        raise typer.BadParameter("--resume requires --run-dir pointing at the run to continue.")

    # Parse tag filters
    include_tags = {t.strip() for t in tags.split(",") if t.strip()} if tags else None
    exclude_tags_set = {t.strip() for t in exclude_tags.split(",") if t.strip()} if exclude_tags else None

    # Translate the surviving alias flags + -D/--set into one validated override
    # map (layer 5). All other task-config knobs are expressed via -D.
    overrides = _build_overrides(
        model=model,
        driver=driver,
        set_overrides=set_overrides,
    )

    # Override API backend if --backend was passed. The flag is shorthand for the
    # API_BACKEND env var, so mirror it into os.environ as well: the docker driver
    # forwards the backend into the container via the standard env passthrough
    # (name-only `--env API_BACKEND`, which reads os.environ). A flag that only
    # mutated `settings` would be dropped at the container boundary and the
    # in-container Settings would silently default to DIRECT.
    if backend is not None:
        from coder_eval.models import ApiBackend

        resolved_backend = ApiBackend(backend)
        settings.api_backend = resolved_backend
        os.environ["API_BACKEND"] = resolved_backend.value

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
                preservation_mode,
                run_dir,
                max_parallel,
                include_tags,
                exclude_tags_set,
                agent_type,
                overrides,
                stream,
                experiment_path=resolved_experiment,
                max_rows=sample,
                sample_per_stratum=sample_per_stratum,
                split=split,
                repeats=repeats,
                verbose=verbose,
                resume=resume,
                include_skipped=include_skipped,
                junit_xml=junit_xml,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Execution interrupted.[/yellow]")
        raise typer.Exit(2) from None


async def _run_all_tasks(
    task_files: list[Path],
    preservation_mode: PreservationMode | None,
    run_dir: Path | None,
    max_parallel: int,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    agent_type: str | None = None,
    overrides: dict[str, Any] | None = None,
    stream_mode: str | None = None,
    experiment_path: Path | None = None,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
    split: str | None = None,
    repeats: int | None = None,
    verbose: bool = False,
    resume: bool = False,
    include_skipped: bool = False,
    junit_xml: Path | None = None,
) -> None:
    """Async entry point for running all tasks (optionally in parallel).

    Tasks are resolved through the experiment layer (defaulting to
    experiments/default.yaml) and executed via run_batch.

    Args:
        task_files: List of task file paths or glob patterns
        preservation_mode: Sandbox preservation mode, or None for the driver-derived default
        run_dir: Custom run directory (or None for auto-generated)
        max_parallel: Maximum number of concurrent tasks
        include_tags: Only run tasks matching any of these tags
        exclude_tags: Skip tasks matching any of these tags
        agent_type: Optional override for agent type (re-parses the union)
        overrides: Generic layer-5 task-config overrides (path -> typed value)
            from -D/--set and the bespoke flag aliases
        stream_mode: Optional stream mode ('full' or 'minimal') for real-time output
        experiment_path: Optional path to experiment YAML (default: experiments/default.yaml)
        split: Optional dataset row filter (--split): keep only rows whose
            dataset.split_field value matches. Applied before max_rows /
            sample_per_stratum; tasks whose rows carry no split label are unaffected.
        junit_xml: Optional path to write a JUnit XML report to, after the run
            summary is persisted and before the failure exit-code gate.
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
        preservation_mode=preservation_mode,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        agent_type=agent_type,
        overrides=overrides or {},
        max_rows=max_rows,
        sample_per_stratum=sample_per_stratum,
        split=split,
        repeats=repeats,
        verbose=verbose,
        include_skipped=include_skipped,
    )

    from ..telemetry import flush_telemetry, track_event

    # TaskFileCount is the pre-expansion file count (dataset fan-out and variant
    # resolution happen later); per-task counts are reconstructable from the
    # CoderEval.Task.End events.
    track_event(
        "CoderEval.Run.Start",
        {
            "TaskFileCount": len(all_task_files),
            "MaxParallel": max_parallel,
            "AgentType": agent_type or "default",
            "StreamMode": stream_mode or "none",
            "Resume": resume,
            "ExperimentProvided": experiment_path is not None,
        },
    )

    # Fail fast if the litellm backend points at an unreachable external proxy —
    # otherwise the agent hangs on the dead endpoint instead of erroring.
    preflight_error = await asyncio.to_thread(_litellm_preflight_error, settings)
    if preflight_error:
        console.print(f"[red]{preflight_error}[/red]")
        raise typer.Exit(1)

    try:
        # Always run through experiment layer (defaults to experiments/default.yaml)
        summary, failed_suite_gates = await _run_with_experiment(
            all_task_files, config, experiment_path, stream_mode, max_parallel, resume=resume
        )

        # Aggregate task logs into run.log
        from ..logging_config import aggregate_task_logs

        aggregate_task_logs(run_dir)

        # Print execution summary
        print_execution_summary(run_dir, summary)

        # Write the JUnit report (if requested) BEFORE the exit-code gate below,
        # so a failing run still produces the report. suite.json + run.json are
        # already on disk (written inside _run_with_experiment). A write error
        # propagates (loud failure, exit != 0) rather than being swallowed.
        if junit_xml is not None:
            from ..reports_junit import write_junit_xml

            written = write_junit_xml(run_dir, junit_xml)
            console.print(f"[green][OK]JUnit report written to {written}[/green]")
    finally:
        # Explicit flush before process exit (belt-and-suspenders with atexit).
        # In a `finally` so it runs on the success path and on any raised
        # exception, but never catches/swallows the typer.Exit decided below.
        flush_telemetry()

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
        rich_renderer = RichStreamRenderer(verbosity=stream_mode, batch_mode=batch_mode)
        logging_renderer = LoggingStreamRenderer()
        stream_callback_factory = lambda _task_id: CompositeStreamCallback([rich_renderer, logging_renderer])  # noqa: E731
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
    resume: bool = False,
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
        resume: Skip tasks already finalized in the run dir, folding their prior
            results back into the summary.

    Returns:
        RunSummary with aggregated results.
    """
    from ..orchestration.batch import (
        clear_rerun_artifacts,
        compute_run_fingerprint,
        fingerprint_diff,
        partition_for_resume,
        read_run_fingerprint,
        run_batch,
        write_run_fingerprint,
    )
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

    # Resolve tasks through experiment layer (applies all 5 config layers).
    # Global failures raise ValueError here — duplicate task IDs, early-stop
    # arming, or an invocation error that trips every task identically (bad
    # --type / -D value, repeats over the cap) — and we surface them as a clean
    # CLI error instead of a traceback. Per-task config-resolution failures
    # (e.g. sdk_options on a non-claude agent) among otherwise-resolvable tasks
    # are NOT raised: resolve_all_tasks isolates them into `skipped` so one
    # incompatible task can't abort the whole suite.
    try:
        resolved, skipped = resolve_all_tasks(
            task_files=all_task_files,
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
            experiment_file=exp_path,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    if skipped:
        console.print(
            f"[yellow]⚠[/] {len(skipped)} task file(s) skipped "
            + "(load errors or `skip: true` — see run.json `skipped_tasks` for reasons)"
        )

    # Warn (don't refuse) when a --resume config differs from the original run. The
    # per-task path key (variant/task_id/NN) doesn't encode the run config, so resumed
    # tasks keep their original-config results — surfacing the mismatch makes the
    # resulting mixed-config run.json visible instead of silent. Best-effort and
    # informational: a missing stamp (run predates this feature) is tolerated.
    current_fingerprint = compute_run_fingerprint(
        config, experiment.experiment_id, settings.api_backend.value, settings.bedrock_model
    )
    if resume:
        prior_fingerprint = read_run_fingerprint(config.run_dir)
        if prior_fingerprint is not None:
            diffs = fingerprint_diff(prior_fingerprint, current_fingerprint)
            if diffs:
                detail = "; ".join(f"{k}: {old!r} → {new!r}" for k, (old, new) in sorted(diffs.items()))
                console.print(
                    f"[yellow]⚠[/] --resume into {config.run_dir} but the run config changed ({detail}). "
                    + "Already-finalized tasks keep their original-config results, so this run mixes "
                    + "configs — use a fresh --run-dir to keep them separate."
                )
    write_run_fingerprint(config.run_dir, current_fingerprint)

    # On --resume, peel off tasks already finalized in the run dir. They are not
    # re-executed but are folded back into run.json (and all downstream reports)
    # via prior_results so the summary covers the whole run. `resolved` stays the
    # full set — suite rollups below need every task, run or not.
    to_run: list[ResolvedTask] = resolved
    prior_results: list[TaskResult] = []
    prior_resolved: list[ResolvedTask] = []
    if resume:
        to_run, prior_results, prior_resolved = partition_for_resume(resolved)
        # A re-run task re-executes from scratch, so any leftover artifacts (only
        # DIRECT_WRITE writes them live; a container killed mid-run leaves partials)
        # are stale and could let a file-based criterion pass on the old output.
        cleared = clear_rerun_artifacts(to_run)
        console.print(
            f"[cyan]↻ Resume:[/] {len(prior_results)} task(s) already complete, "
            + f"running {len(to_run)} remaining"
            + (f" (cleared {cleared} stale artifact dir(s))" if cleared else "")
        )

    # Print execution mode
    print_execution_mode(len(to_run), max_parallel)

    summary, task_results = await _run_with_callbacks(
        execute_fn=lambda **kwargs: run_batch(
            resolved_tasks=to_run,
            config=config,
            skipped_tasks=skipped,
            prior_results=prior_results,
            prior_resolved=prior_resolved,
            **kwargs,
        ),
        task_count=len(to_run),
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
