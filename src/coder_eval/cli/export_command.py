"""``coder-eval export`` — emit a task in another framework's native format.

Currently one target: ``--format harbor`` (C2). This is a *writer* only —
``--format`` is reserved so the same flag can later grow a *reader*
(``coder-eval run <harbor-task>``, demoted — see ``tmp/harborframework.partB.md``)
without a naming collision.

Two modes, told apart by whether ``-e/--experiment`` is passed:

- Single task.yaml → one Harbor task directory at ``-o``, unchanged from C2.
- One or more task.yaml files + ``-e experiment.yaml`` → the experiment's own
  ``resolve_all_tasks`` pipeline resolves every (task, variant, replicate[,
  dataset row]) combination, and each is written to its own subdirectory under
  ``-o`` (``<variant_id>/<task_id>/[<row_id>/]rep<NN>/``). See
  ``tmp/harborframework_conversion.md`` for what an experiment-introduced
  override this export cannot honor looks like, and why.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..harbor.experiment_packager import export_experiment
from ..harbor.packager import CriteriaNotExportableError, TaskNotExportableError, export_task
from .console import console
from .run_helpers import expand_task_files


_SUPPORTED_FORMATS = ("harbor",)


def export_command(
    task_files: list[Path] = typer.Argument(  # noqa: B008
        ...,
        help="The coder-eval task YAML(s) to export (glob patterns allowed with --experiment).",
    ),
    output_dir: Path = typer.Option(  # noqa: B008
        ...,
        "--output",
        "-o",
        help="Directory to write the exported task(s) into (created if missing).",
        file_okay=False,
    ),
    experiment: Path | None = typer.Option(  # noqa: B008
        None,
        "--experiment",
        "-e",
        help=(
            "Experiment definition YAML. Resolves every (task, variant, replicate[, dataset row]) "
            "combination via the same pipeline `coder-eval run -e` uses, and exports each as its own "
            "Harbor directory under --output."
        ),
        exists=True,
        dir_okay=False,
    ),
    format: str = typer.Option(
        "harbor",
        "--format",
        help=f"Target format. Supported: {', '.join(_SUPPORTED_FORMATS)}.",
    ),
    allow_credentials: bool = typer.Option(
        False,
        "--allow-credentials",
        help=(
            "Export criteria that need model credentials/network inside the verifier "
            "(llm_judge / agent_judge / uipath_eval) anyway. Only pass this if you have "
            "already provisioned that access yourself — the export does not do it for you."
        ),
    ),
) -> None:
    """Export a coder-eval task (or task x experiment.yaml variants) to another framework's directory format.

    Examples:
        coder-eval export tasks/my_task.yaml -o dist/harbor/my_task --format harbor
        coder-eval export tasks/my_task.yaml -e experiments/model-comparison.yaml -o dist/harbor/my_experiment
    """
    if format not in _SUPPORTED_FORMATS:
        console.print(f"[red]✗[/] Unsupported --format {format!r}. Supported: {', '.join(_SUPPORTED_FORMATS)}.")
        raise typer.Exit(1)

    if experiment is None:
        if len(task_files) != 1:
            console.print("[red]✗[/] Without --experiment, pass exactly one task YAML.")
            raise typer.Exit(1)
        try:
            result = export_task(task_files[0], output_dir, allow_credentials=allow_credentials)
        except (TaskNotExportableError, CriteriaNotExportableError) as e:
            console.print(f"[red]✗[/] {e}")
            raise typer.Exit(1) from e
        console.print(f"[green]✓[/] Exported {task_files[0]} → {result.out_dir} (format: {format})")
        for warning in result.warnings:
            console.print(f"[yellow]⚠[/] {warning}")
        return

    all_task_files = expand_task_files(task_files)
    exp_result = export_experiment(
        all_task_files,
        experiment,
        output_dir,
        allow_credentials=allow_credentials,
    )

    for exported in exp_result.exported:
        console.print(f"[green]✓[/] Exported → {exported.out_dir} (format: {format})")
        for warning in exported.warnings:
            console.print(f"[yellow]⚠[/] {warning}")
    for skip in exp_result.skipped:
        console.print(
            f"[yellow]⚠[/] Skipped variant={skip.variant_id!r} task={skip.task_id!r} "
            + f"rep={skip.replicate_index}: {skip.reason}"
        )
    for load_skip in exp_result.load_skipped:
        console.print(f"[yellow]⚠[/] Skipped task load: {load_skip}")

    console.print(
        f"[bold]{len(exp_result.exported)}[/] directories exported, "
        + f"[bold]{len(exp_result.skipped)}[/] variant(s) skipped, "
        + f"[bold]{len(exp_result.load_skipped)}[/] task file(s) skipped at load."
    )
    if not exp_result.exported:
        raise typer.Exit(1)


__all__ = ["export_command"]
