"""``coder-eval export`` — emit a task in another framework's native format.

Currently one target: ``--format harbor`` (C2). This is a *writer* only —
``--format`` is reserved so the same flag can later grow a *reader*
(``coder-eval run <harbor-task>``, demoted — see ``tmp/harborframework.partB.md``)
without a naming collision.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..harbor.packager import CriteriaNotExportableError, TaskNotExportableError, export_task
from .console import console


_SUPPORTED_FORMATS = ("harbor",)


def export_command(
    task_file: Path = typer.Argument(  # noqa: B008
        ...,
        help="The coder-eval task YAML to export.",
        exists=True,
        dir_okay=False,
    ),
    output_dir: Path = typer.Option(  # noqa: B008
        ...,
        "--output",
        "-o",
        help="Directory to write the exported task into (created if missing).",
        file_okay=False,
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
    """Export a coder-eval task to another framework's native directory format.

    Examples:
        coder-eval export tasks/my_task.yaml -o dist/harbor/my_task --format harbor
    """
    if format not in _SUPPORTED_FORMATS:
        console.print(f"[red]✗[/] Unsupported --format {format!r}. Supported: {', '.join(_SUPPORTED_FORMATS)}.")
        raise typer.Exit(1)

    try:
        result = export_task(task_file, output_dir, allow_credentials=allow_credentials)
    except (TaskNotExportableError, CriteriaNotExportableError) as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1) from e

    console.print(f"[green]✓[/] Exported {task_file} → {result.out_dir} (format: {format})")
    for warning in result.warnings:
        console.print(f"[yellow]⚠[/] {warning}")


__all__ = ["export_command"]
