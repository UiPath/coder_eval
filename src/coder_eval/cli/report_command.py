"""Report command - display or export evaluation results."""

from pathlib import Path

import typer
from rich.markdown import Markdown

from ..models import EvaluationResult
from ..reports import ReportGenerator
from ..reports_html import write_task_html
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
        help="Output file (default: display markdown to stdout).",
    ),
    report_format: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: 'md' (default markdown), 'html' (render task.json files as HTML).",
    ),
) -> None:
    """Display or export a run report.

    The run command automatically generates a report during execution.
    This command allows you to view or export that report later.

    Examples:
        # Display latest run report
        coder-eval report runs/latest

        # Export specific run to markdown file
        coder-eval report runs/2025-10-10_20-57-30 -o summary.md

        # Re-generate HTML reports for every task.json under a run dir
        coder-eval report runs/latest --format html
    """
    fmt = report_format.lower()
    if fmt not in ("md", "html"):
        console.print(f"[red]Error: unknown --format '{report_format}' (expected 'md' or 'html')[/red]")
        raise typer.Exit(1)

    if fmt == "html":
        _regenerate_html_reports(run_dir, output_file)
        return

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
        console.print(f"[green][OK]Report saved to {output_file}[/green]")
    else:
        console.print(Markdown(report_md))


def _regenerate_html_reports(run_dir: Path, output_file: Path | None) -> None:
    """Regenerate task-level HTML reports from every task.json under run_dir.

    If `output_file` is provided and exactly one task.json is found, the
    report is written to that file. Otherwise each task.html is written
    next to its task.json.
    """
    task_json_paths = sorted(run_dir.rglob("task.json"))
    if not task_json_paths:
        console.print(f"[red]Error: no task.json files found under {run_dir}[/red]")
        raise typer.Exit(1)

    if output_file and len(task_json_paths) > 1:
        msg = (
            f"[red]Error: --output targets a single file but {len(task_json_paths)} task.json files "
            + "were found. Omit --output to regenerate all reports in place.[/red]"
        )
        console.print(msg)
        raise typer.Exit(1)

    from ..evaluation.judge_persistence import load_judge_transcripts

    written: list[Path] = []
    failed: list[Path] = []
    for task_json in task_json_paths:
        try:
            result = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[yellow]Skipping {task_json}: {e}[/yellow]")
            failed.append(task_json)
            continue
        # Pull any spilled judge transcripts back onto the in-memory result so
        # the HTML re-render shows the same disclosures the original run produced.
        load_judge_transcripts(result, task_json.parent)
        target = output_file if output_file else task_json.with_name("task.html")
        written_path = write_task_html(result, target)
        if written_path is None:
            console.print(f"[red]Failed to write HTML for {task_json}[/red]")
            failed.append(task_json)
            continue
        written.append(written_path)

    for p in written:
        console.print(f"[green][OK]Wrote {p}[/green]")
    console.print(f"[green]Generated {len(written)} HTML report(s)[/green]")
    if failed:
        console.print(f"[red]{len(failed)} task(s) failed[/red]")
        raise typer.Exit(1)
