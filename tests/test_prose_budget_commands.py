"""`prose_budget`'s Typer-command exemption must name exactly the commands the CLI registers."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import typer.main

import coder_eval
from coder_eval.cli import app
from tests.lint.prose_budget import _TYPER_COMMANDS


def _callbacks(command: click.Command) -> Iterator[Any]:
    if isinstance(command, click.Group):
        for sub in command.commands.values():
            yield from _callbacks(sub)
    elif command.callback is not None:
        yield inspect.unwrap(command.callback)


def test_every_exemption_names_a_registered_command_and_back() -> None:
    """A deleted command left its exemption behind with nothing to flag it; a new command
    without one has its docstring budgeted as prose. Both directions fail here."""
    src = Path(coder_eval.__file__).resolve().parent
    registered = {
        (Path(inspect.getfile(fn)).resolve().relative_to(src).as_posix(), fn.__name__)
        for fn in _callbacks(typer.main.get_command(app))
    }

    assert registered == set(_TYPER_COMMANDS)
