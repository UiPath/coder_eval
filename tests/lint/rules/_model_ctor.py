"""Resolve `coder_eval.models` constructor calls inside one module's AST.

CE060 and CE061 ask the same first question — *is this call building an
`AssistantMessage`?* — and answering it takes more than matching a name: a
module may bind the class under any alias, reach it through a relative import,
or never bind it at all and spell it `models.AssistantMessage(...)`. CE060
worked that out once; duplicating it into CE061 would mean a model rename or a
new import spelling needs two fixes in two rules, and the second one is the one
that gets missed. So it lives here and both rules consume it.

The class name is taken from the model itself rather than written as a string,
the way CE056 imports `IN_CONTAINER_ENV` and CE057 derives its target set from
`SIDECAR_MODULES`: renaming the model moves both rules with it.

BLIND SPOT, inherited by every consumer: a re-export through an intermediate
module (`from .sibling import AssistantMessage`) is invisible, because
resolving it means following imports across files and no rule in this package
does that.
"""

import ast
import re

from coder_eval.models import AssistantMessage


# Reducers live here; nothing outside it builds a generation window.
AGENTS_ROOT = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]agents[/\\]")

_MODELS_MODULE = "coder_eval.models"
# Taken from the model, never spelled here: a rename then moves the rules too.
ASSISTANT_MESSAGE = AssistantMessage.__name__


def reaches_module(node: ast.ImportFrom, module_path: str) -> bool:
    """True if this `from ... import` reaches `module_path`.

    A relative import inside `agents/` (`from ..models import ...`) carries only
    the tail in `node.module`, so testing the absolute path alone would leave a
    rule silently blind for a whole file — and `agents/` does use relative
    imports.
    """
    module = node.module or ""
    if module.startswith(module_path):
        return True
    if not node.level:
        return False
    # A relative spelling carries only a TRAILING SLICE of the absolute path,
    # and how much of it depends on the dot count: `from ..timing import` gives
    # "timing", `from ..streaming.events import` gives "streaming.events". So
    # match any suffix of the target, segment-wise, allowing the import to
    # continue on into a submodule below it (`..models.criteria`). Comparing a
    # single `rpartition` tail was right only while every target was
    # one segment deep; it silently missed `coder_eval.streaming.events`
    # entirely, which is a rule blind for a whole file rather than a near miss.
    segments = module_path.split(".")
    spelled = module.split(".")
    return any(spelled[: len(segments) - i] == segments[i:] for i in range(1, len(segments)))


def reaches_models_module(node: ast.ImportFrom) -> bool:
    """`reaches_module` pinned to `coder_eval.models` — CE060/CE061's question."""
    return reaches_module(node, _MODELS_MODULE)


def bindings_from(tree: ast.AST, class_name: str, module_path: str) -> set[str]:
    """Every local name this module binds `<module_path>.<class_name>` to.

    Built per file: caching it across files would leak one module's alias into
    another's matching.

    Parameterized on the module because CE064 asks the identical question about
    `coder_eval.streaming.events` and `coder_eval.timing` rather than about
    `coder_eval.models`. Copying the resolver into it would mean a new import
    spelling needs three fixes in three rules, and the third is the one that
    gets missed — which is the argument this file already makes for CE060 and
    CE061 sharing it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and reaches_module(node, module_path):
            names.update(a.asname or a.name for a in node.names if a.name == class_name)
    return names


def local_bindings(tree: ast.AST, class_name: str) -> set[str]:
    """`bindings_from` pinned to `coder_eval.models` — CE060/CE061's question."""
    return bindings_from(tree, class_name, _MODELS_MODULE)


def constructor_name(func: ast.expr, names: set[str], class_name: str) -> str | None:
    """The spelling this call used to name the model, or None if it did not.

    A bare name has to be bound in this module to be ours; the attribute
    spelling is matched on the attribute alone, since the module binding it
    arrives through (`import coder_eval.models as models`, `from coder_eval
    import models`) is what a walk over one file's CLASS bindings cannot see.
    """
    if isinstance(func, ast.Name) and func.id in names:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr == class_name:
        return func.attr
    return None


def keywords_of(node: ast.Call) -> dict[str, ast.expr]:
    """The call's named arguments. A `**`-expansion contributes nothing.

    That is deliberate rather than an oversight: such a call has not declared
    the field AT THE SITE, which is what these rules are about.
    """
    return {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}


def is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None
