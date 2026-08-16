"""The harness's ONE reader of Typer's resolved ``OptionInfo`` — shared by CE043 and CE046.

Two rules ask questions about the same declarations: CE043 asks whether ``run`` and ``plan``
declare the row selectors identically, CE046 whether every long flag reaches
``docs/USER_GUIDE.md``. Reading ``param_decls`` two ways is precisely the drift CE043 exists to
prevent, one level up — so both read it here.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.** These functions read
``OptionInfo.param_decls``, which holds only the flags a command declares EXPLICITLY. A flag Typer
derives from the parameter name (``sample: int | None = typer.Option(None)`` becoming ``--sample``)
carries no ``param_decls`` and is invisible here — the same blind spot CE043 already declares.
Short flags are filtered out by construction. Nothing here inspects behaviour: a command may
declare a flag and ignore it entirely.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def long_flags(fn: Callable[..., Any]) -> set[str]:
    """Every ``--long`` flag the command declares, read off its Typer OptionInfo defaults."""
    out: set[str] = set()
    for param in inspect.signature(fn).parameters.values():
        out.update(getattr(param.default, "param_decls", ()) or ())
    return {flag for flag in out if flag.startswith("--")}


def help_for(fn: Callable[..., Any], flag: str) -> str | None:
    """The help STRING OBJECT the command attaches to ``flag``, or ``None``.

    Callers compare it with ``is`` rather than ``==``: an inlined copy of the same sentence would
    compare equal while being a second declaration, free to drift on the next edit.
    """
    for param in inspect.signature(fn).parameters.values():
        if flag in (getattr(param.default, "param_decls", ()) or ()):
            return getattr(param.default, "help", None)
    return None


def documented_commands() -> dict[str, Callable[..., Any]]:
    """The user-facing CLI surfaces: every non-hidden command plus the root callback.

    DERIVED from ``coder_eval.cli.app`` rather than enumerated. A hand-written list would be a
    second declaration of "this command is part of the user surface", which ``hidden=True`` in
    ``cli/__init__.py`` already states once — and it is easy to miss a surface by hand
    (``evaluate`` and ``aggregate`` both are).

    ``hidden`` is a real bool on a registered command but a Typer ``DefaultPlaceholder`` on the
    root callback, so anything that is not literally ``True`` counts as visible.
    """
    from coder_eval.cli import app

    commands: dict[str, Callable[..., Any]] = {}
    for command in app.registered_commands:
        if command.callback is None or command.hidden is True:
            continue
        commands[command.name or command.callback.__name__] = command.callback
    callback = app.registered_callback
    if callback is not None and callback.callback is not None and callback.hidden is not True:
        commands[callback.callback.__name__] = callback.callback
    return commands


def undocumented_flags(commands: dict[str, Callable[..., Any]], guide_text: str) -> list[str]:
    """Every declared long flag whose bare name is absent from ``guide_text``.

    A ``--x/--no-x`` boolean pair arrives from ``param_decls`` as ONE unspaced string while the
    guide writes it with spaces around the slash, so the decl is split on ``/`` and each bare name
    matched on its own. That is required behaviour rather than leniency: the raw-substring form
    fails on ``--preserve/--no-preserve``, which IS documented — a sensor demanding an edit that
    makes the guide worse is the worst kind.
    """
    missing: list[str] = []
    for name, fn in sorted(commands.items()):
        for flag in sorted(long_flags(fn)):
            absent = [bare for bare in flag.split("/") if bare and bare not in guide_text]
            if absent:
                missing.append(
                    f"`{name} {flag}` is not in docs/USER_GUIDE.md (missing: {', '.join(absent)}). "
                    "That guide is the flag-table SSOT, so documenting it in another page does not "
                    "count — either add a row there, or mark the command `hidden=True` in "
                    "cli/__init__.py if it is not part of the user-facing surface."
                )
    return missing
