"""Report command - display or export evaluation results."""

from pathlib import Path

import typer
from rich.markdown import Markdown

from ..reports import ReportGenerator
from .console import console


def report_command(
    run_dir: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to a run directory (e.g., runs/latest or runs/2025-10-10_20-57-30)",
        exists=True,
    ),
    output_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output file for markdown report (default: display to stdout)",
    ),
) -> None:
    """Display or export a run report.

    The run command automatically generates a report during execution.
    This command allows you to view or export that report later.

    Examples:
        # Display latest run report
        coder-eval report runs/latest

        # Export specific run to file
        coder-eval report runs/2025-10-10_20-57-30 -o summary.md

        # View older run
        coder-eval report runs/2025-10-09_15-30-45
    """
    try:
        report_md, source_path = ReportGenerator.load_from_run_dir(run_dir)
        console.print(f"[dim]Reading report from {source_path}[/dim]\n")

    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("\n[dim]Hint: Use 'coder-eval run' to create evaluation runs.[/dim]")
        raise typer.Exit(1) from e

    # Output report
    if output_file:
        output_file.write_text(report_md)
        console.print(f"[green]✓ Report saved to {output_file}[/green]")
    else:
        console.print(Markdown(report_md))
