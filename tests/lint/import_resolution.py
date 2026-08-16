"""The ONE relative-import resolver every ``node.module``-matching lint rule routes through.

**The failure this exists to stop, measured.** Five rules in ``tests/lint/rules/`` inspect imports
and every one of them matched on ``node.module`` alone. For a RELATIVE import — ``from ..models
import X`` — ``node.module`` is ``"models"`` and ``node.level`` is ``2``, so a pattern anchored on
``coder_eval.models`` does not match and the rule reports nothing. Fed the absolute and the
relative spelling of one violation, CE001 and CE041 both fired on the first and were silent on the
second; CE041 reported **0 violations against 8 real model-constructor splats in ``src/``** and had
never fired in its life.

That is the dangerous shape: the rule fails **OPEN**. A broken import rule and a clean codebase are
indistinguishable from the outside — both print nothing — so four rules stayed broken at once while
``make lint`` stayed green. CE051 is the meta-rule that keeps a fifth from joining them, and it
keys on "does this rule call ``resolved_module``" rather than on "does it mention ``node.level``",
because that is the DRY rule and the correctness rule at the same time and cannot be satisfied by a
stray mention.

Shared, non-numbered helper beside ``cli_flags.py`` and ``markdown_tables.py``, which set the
precedent for a reader several rules depend on.
"""

import ast
from pathlib import Path


_PACKAGE_ROOT = "coder_eval"


def resolved_module(node: ast.ImportFrom, filepath: str) -> str | None:
    """The absolute dotted module an ``ImportFrom`` refers to, resolving ``node.level``.

    ``level == 0`` is an absolute import and passes ``node.module`` straight through (``None`` for
    the degenerate ``from . import x`` at level 0, which the grammar does not produce). A relative
    import is resolved against the importing file's own package: ``level == 1`` is the containing
    package, ``level == 2`` its parent, and so on.

    Returns ``None`` — and never a half-resolved string — when the answer cannot be known:

    * the file's path does not sit under a ``coder_eval/`` package root, so there is no package to
      resolve against (a fixture written to a ``tmp_path``, say);
    * the relative import walks ABOVE that root (``from ....x import y`` three levels up from
      ``coder_eval/cli/``).

    Callers degrade to "no match" on ``None`` rather than guessing. That direction is deliberate:
    an import this resolver cannot place is one a rule should not fire on, because the cost of
    guessing wrong is a false violation on code that is fine.

    ``from . import models`` (``node.module is None``, ``level == 1``) resolves to the PACKAGE
    itself — ``coder_eval.orchestration`` for a file in that directory — and the imported *name* is
    then a submodule rather than a symbol. Rules matching on the module string see the package, not
    ``coder_eval.orchestration.models``; a rule that cares about the imported names has to read
    ``node.names`` itself, which is stated here because the distinction is invisible at the call
    site.
    """
    if node.level == 0:
        return node.module

    package = _package_chain(filepath)
    if package is None:
        return None
    package.pop()  # drop the module's own name; imports resolve against its package

    # `level == 1` is that package; each extra level walks one more up.
    for _ in range(node.level - 1):
        if not package:
            return None  # walked above the package root
        package.pop()
    if not package:
        return None
    return ".".join([*package, node.module]) if node.module else ".".join(package)


def _package_chain(filepath: str) -> list[str] | None:
    """The dotted parts from the ``coder_eval`` package root down to the module, or ``None``.

    Two steps, and the second is what makes the answer trustworthy rather than merely plausible.

    **Innermost root.** ``rindex`` rather than ``index``: this repo's own layout is
    ``…/coder_eval/src/coder_eval/…``, so the FIRST match is the checkout directory and resolving
    against it would put the whole source tree into the module path.

    **The chosen root must really be a package.** A path can contain a ``coder_eval`` directory
    that is not the package — the checkout root, again — and for any file outside ``src/coder_eval``
    the lexical rule alone then fabricates a module path rather than declining. Measured, before
    this check existed: ``…/coder_eval/tests/lint/rules/foo.py`` + ``from ..models import X``
    returned ``"coder_eval.tests.lint.models"``, and ``…/coder_eval/evalboard/scripts/f.py``
    returned ``"coder_eval.evalboard.models"``. Both are confident, both are fiction, and a
    fabricated string is strictly worse than ``None``: it can make a rule fire on innocent code or
    stay silent on a real violation, which is the failure this whole helper was written to end.
    Requiring ``__init__.py`` at the chosen root settles it with one stat call — the checkout root
    has none, the package does.

    The consequence for callers, stated because it is easy to trip over: a path that does not
    EXIST is not resolvable either, so a rule tested against a synthetic filepath must use one
    under the real ``src/coder_eval/`` tree. That is what the tests do, and it is why they anchor
    on an absolute path rather than a CWD-relative one.
    """
    try:
        parts = Path(filepath).with_suffix("").parts
    except ValueError:
        # `Path("")`, `Path("/")` and `Path(".")` have no name for `with_suffix` to replace.
        # Unreachable through the runner, which always passes a real `.py` path, but a resolver
        # whose whole contract is to degrade should not be the thing that raises.
        return None
    if _PACKAGE_ROOT not in parts:
        return None
    root_index = len(parts) - 1 - parts[::-1].index(_PACKAGE_ROOT)
    if not (Path(*parts[: root_index + 1]) / "__init__.py").exists():
        return None
    return list(parts[root_index:])
