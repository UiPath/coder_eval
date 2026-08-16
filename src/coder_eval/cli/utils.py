"""Shared utility functions for CLI commands."""

import shutil

from rich.markup import escape

from ..config import settings
from .console import console


def check_tools() -> None:
    """Check that required tools are available."""
    console.print("[bold]Checking required tools...[/bold]")

    tools = {
        "claude": "Claude Code CLI",
        "uv": "UV package manager",
    }

    all_found = True
    for cmd, name in tools.items():
        if shutil.which(cmd):
            console.print(f"  [green]✓[/green] {escape(str(name))} ({escape(str(cmd))})")
        else:
            console.print(f"  [red]✗[/red] {escape(str(name))} ({escape(str(cmd))}) not found")
            all_found = False

    if not all_found:
        console.print("[yellow]Warning: Some tools are missing[/yellow]")


def check_api_keys() -> None:
    """Check that API keys are configured."""
    console.print("\n[bold]Checking API keys...[/bold]")

    if settings.anthropic_api_key:
        console.print("  [green]✓[/green] ANTHROPIC_API_KEY is set")
    else:
        console.print("  [yellow]⚠[/yellow] ANTHROPIC_API_KEY not set")
