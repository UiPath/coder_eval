"""CE042: a replicate directory's NAME is spelled in exactly one place — ``path_utils.py``.

``path_utils.replicate_subdir_name`` owns the two-digit zero-padded replicate directory name, and
its own docstring already says so: *"Callers MUST use this helper rather than hand-rolling the
f-string so future format changes (e.g., NN → NNN) touch exactly one place."* Four sites outside it
hand-rolled the padding anyway — two ``f"{replicate_index:02d}"`` spellings in ``reports_junit.py``,
a ``glob("*/[0-9][0-9]/task.json")`` in ``optimize/gate.py`` and a ``glob("[0-9][0-9]")`` in
``reports_stats.py``.

The day that padding widens, none of them raises. The globs simply match nothing: **both optimize
gates load ZERO rows**, and the zero-row note then blames a wrong variant id, a wrong suite id or a
wrong run directory — sending a reader to check the one thing that is correct.

**Why a rule rather than a shared constant.** The three readers glob at different depths —
``optimize/gate.py`` two levels down from a *suite* directory, ``reports_junit`` / ``reports_stats``
one level down from a *task* directory — so a single shared glob constant would have to be
concatenated at two of the three sites, which is the duplication again wearing a constant's name.
The invariant being protected is *"a replicate directory's name is not a pattern any reader may
pin"*, and a rule is the instrument for that, exactly as CE040 protects the bootstrap's p-floor with
a function rather than a literal.

**What it detects, precisely — two shapes:**

1. An f-string field whose format spec is ``02d`` **and whose formatted value is a name or
   attribute containing "replicate"** (``f"{replicate_index:02d}"``, ``f"{row.replicate_index:02d}"``).
   Keying on the NAME is CE040's lesson: an unconditional ``02d`` match would flag
   ``f"{minutes:02d}:{seconds:02d}"`` and tell its author to call ``replicate_subdir_name``.
2. A string constant containing ``[0-9][0-9]`` — unconditional, because that glob character class
   has exactly one meaning in this tree.

The boundary, stated so a green ``make lint`` is not mistaken for a proof: ``zfill(2)``,
``"%02d" %``, a ``??`` glob, and a padding built from a differently-named local are NOT matched.
The scan is ``src/`` only, because a test that writes a fixture run tree legitimately spells the
padding it is fabricating.

**Shape 2 is unconditional, and that cuts both ways.** ``[0-9][0-9]`` in *any* string constant
fires — including inside a docstring, which is a string constant like any other. That is deliberate
for prose pinning the old glob (documentation that lies after the padding widens is the same defect
one surface along), but it also means a genuine non-replicate regex that happens to contain the
class — a ``HH:MM`` timestamp pattern, say — would fire as a false positive. Nothing in ``src/``
does today. That one case is what ``# noqa: CE042`` plus a comment saying why is for; it is the
only intended use of a suppression here.

The intended fix everywhere else is to call ``path_utils.replicate_subdir_name(index)`` when
BUILDING a path, or to glob padding-agnostically (``*/task.json``) when READING one.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CANONICAL_MODULE = re.compile(r"[/\\]path_utils\.py$")
_PADDED_GLOB = "[0-9][0-9]"
_REPLICATE = "replicate"

_FIX = (
    "the replicate directory's two-digit padding is hand-rolled here. "
    "path_utils.replicate_subdir_name owns that name, and pinning the padding elsewhere makes "
    "every such reader silently match nothing the day it widens — the optimize gates load zero "
    "rows and blame a path typo. Call path_utils.replicate_subdir_name(index) to BUILD a "
    "replicate path, or glob padding-agnostically (`*/task.json`) to READ one."
)


def _is_replicate_value(node: ast.expr) -> bool:
    """True when the formatted value NAMES a replicate.

    Keyed on the name rather than on the ``02d`` shape alone: two-digit padding is ordinary
    formatting, and a rule that flagged ``f"{minutes:02d}"`` would tell its author to reach for a
    replicate helper. This is CE040's narrowness, applied to the other half of the pair.
    """
    if isinstance(node, ast.Name):
        return _REPLICATE in node.id.lower()
    if isinstance(node, ast.Attribute):
        return _REPLICATE in node.attr.lower()
    return False


def _is_two_digit_spec(node: ast.FormattedValue) -> bool:
    spec = node.format_spec
    if not isinstance(spec, ast.JoinedStr):
        return False
    return any(isinstance(part, ast.Constant) and part.value == "02d" for part in spec.values)


class ReplicatePaddingSeam(BaseRule):
    id = "CE042"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._is_canonical = bool(_CANONICAL_MODULE.search(filepath))

    def visit_FormattedValue(self, node: ast.FormattedValue) -> None:
        if not self._is_canonical and _is_two_digit_spec(node) and _is_replicate_value(node.value):
            self.violation(node, _FIX)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not self._is_canonical and isinstance(node.value, str) and _PADDED_GLOB in node.value:
            self.violation(node, _FIX)
        self.generic_visit(node)
