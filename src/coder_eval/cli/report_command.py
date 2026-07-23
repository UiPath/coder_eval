"""Report command - display or export evaluation results."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

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
        help=(
            "Output format: 'md' (default markdown), 'html' (render task.json files as HTML), "
            "'atif' (regenerate trajectory.json in ATIF format next to each task.json)."
        ),
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

        # Backfill ATIF trajectory.json for every task.json under a run dir
        coder-eval report runs/latest --format atif
    """
    fmt = report_format.lower()
    if fmt not in ("md", "html", "atif"):
        console.print(f"[red]Error: unknown --format '{report_format}' (expected 'md', 'html', or 'atif')[/red]")
        raise typer.Exit(1)

    if fmt == "html":
        _regenerate_html_reports(run_dir, output_file)
        return

    if fmt == "atif":
        if output_file:
            console.print(
                "[red]Error: --output is not supported with --format atif; "
                + "trajectory.json is written next to each task.json.[/red]"
            )
            raise typer.Exit(1)
        _regenerate_atif_trajectories(run_dir)
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
        output_file.write_text(report_md, encoding="utf-8")
        console.print(f"[green][OK]Report saved to {output_file}[/green]")
    else:
        console.print(Markdown(report_md))


def _sweep_task_jsons(
    run_dir: Path,
    writer: Callable[[EvaluationResult, Path], Path | None],
    *,
    artifact_label: str,
    on_none: Literal["fail", "skip"],
) -> None:
    """Walk every task.json under ``run_dir`` and feed it to ``writer``.

    Shared by the ``html`` and ``atif`` branches: rglob discovery, empty-dir
    error, per-file parse-warn-continue, and the written/failed tally.

    ``writer`` receives the parsed result and the task.json path and returns
    the written artifact path, or None — treated per ``on_none``: ``"fail"``
    (the html contract: a None write is an error) or ``"skip"`` (the atif
    contract: zero-step results legitimately produce no trajectory).
    """
    task_json_paths = sorted(run_dir.rglob("task.json"))
    if not task_json_paths:
        console.print(f"[red]Error: no task.json files found under {run_dir}[/red]")
        raise typer.Exit(1)

    written: list[Path] = []
    failed: list[Path] = []
    for task_json in task_json_paths:
        try:
            result = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[yellow]Skipping {task_json}: {e}[/yellow]")
            failed.append(task_json)
            continue
        try:
            written_path = writer(result, task_json)
        except Exception as e:
            # A raising writer is always a failure — never collapsed into the
            # legitimate None-skip below (the backfill's strict writer raises
            # on converter bugs precisely so they surface here).
            console.print(f"[red]Failed to write {artifact_label} for {task_json}: {e}[/red]")
            failed.append(task_json)
            continue
        if written_path is None:
            if on_none == "fail":
                console.print(f"[red]Failed to write {artifact_label} for {task_json}[/red]")
                failed.append(task_json)
            else:
                console.print(f"[dim]Skipped {task_json} (no {artifact_label} to produce)[/dim]")
            continue
        written.append(written_path)

    for p in written:
        console.print(f"[green][OK]Wrote {p}[/green]")
    console.print(f"[green]Generated {len(written)} {artifact_label}(s)[/green]")
    if failed:
        console.print(f"[red]{len(failed)} task(s) failed[/red]")
        raise typer.Exit(1)


def _regenerate_html_reports(run_dir: Path, output_file: Path | None) -> None:
    """Regenerate task-level HTML reports from every task.json under run_dir.

    If `output_file` is provided and exactly one task.json is found, the
    report is written to that file. Otherwise each task.html is written
    next to its task.json.
    """
    if output_file and len(sorted(run_dir.rglob("task.json"))) > 1:
        msg = (
            "[red]Error: --output targets a single file but multiple task.json files "
            + "were found. Omit --output to regenerate all reports in place.[/red]"
        )
        console.print(msg)
        raise typer.Exit(1)

    from ..evaluation.judge_persistence import load_judge_transcripts

    def _write_html(result: EvaluationResult, task_json: Path) -> Path | None:
        # Pull any spilled judge transcripts back onto the in-memory result so
        # the HTML re-render shows the same disclosures the original run produced.
        load_judge_transcripts(result, task_json.parent)
        target = output_file if output_file else task_json.with_name("task.html")
        return write_task_html(result, target)

    _sweep_task_jsons(run_dir, _write_html, artifact_label="HTML report", on_none="fail")


def _regenerate_atif_trajectories(run_dir: Path) -> None:
    """Backfill ATIF trajectory.json next to every task.json under run_dir.

    Reuses the exact converter the orchestrator uses at run time, so a
    backfilled trajectory is identical to what the run would have emitted.
    Zero-step results (e.g. evaluate-only runs) are skipped, not failed —
    but converter/write errors RAISE (strict writer) so the sweep reports
    them as failures instead of silently collapsing them into skips.
    """
    from ..harbor import write_trajectory_json_strict

    def _write_atif(result: EvaluationResult, task_json: Path) -> Path | None:
        return write_trajectory_json_strict(result, task_json.with_name("trajectory.json"))

    _sweep_task_jsons(run_dir, _write_atif, artifact_label="trajectory", on_none="skip")
