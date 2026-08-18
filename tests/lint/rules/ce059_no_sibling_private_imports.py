"""CE059 — inside ``coder_eval/optimize/``, a name shared between siblings may not be private.

One underscore cannot mark two boundaries. The family has three tiers — the skill-facing API the
``SKILL.md`` snippets import, the package-internal helpers several modules share, and the names
local to one file — and a leading underscore is the only marker available. Before the package
landed, the middle tier was spelled like the innermost: 29 names carrying an underscore while four
modules imported them, which tells a reader "safe to change this signature" about a helper a change
here breaks in three other files. After the rename the middle tier is spelled like the outermost,
which tells a reader "the skill might depend on this" about a helper nothing outside the package
imports. The second error costs caution; the first costs a build the author did not expect.

So: a name two modules in this package share is PUBLIC, and only a file-local name keeps the
underscore. This rule is what stops the convention decaying, and the decay is silent — the next
author adding a cross-module helper has even odds of prefixing it out of habit, and nothing fails.

**Why not ruff's ``PLC2701`` (*import-private-name*), measured.** Pointed at the module that imported
11 private names from two siblings, it reports "All checks passed": it fires only on a private import
from an *external* module, and same-package imports are outside its scope by design. Across all of
``src/`` it finds 4 violations, every one a third-party import, and it is preview-only. It answers a
different question and cannot be enabled instead.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.**

- It matches ``ImportFrom`` only. ``import coder_eval.optimize.load as load`` followed by
  ``load._PairedRows``, or a ``getattr(module, "_name")``, is invisible to it.
- It matches an imported NAME, so it is blind to a private type reached without importing it: a
  public function returning a private ``NamedTuple`` is a cross-module signature this rule does not
  see, and a sibling then cannot annotate it without tripping the rule. ``_PairedRows`` and
  ``_HolmFamily`` are that shape today — deliberately, since no sibling imports either — and the
  case is recorded in ``.claude/harness-candidates.md`` rather than papered over here.
- It says nothing about whether a name that IS public *should* be — that is a review question. The
  rule enforces the marker, not the design.
- A private MODULE is out of reach: ``from coder_eval.optimize import _helpers`` resolves to the
  package itself, not to a sibling, so the prefix test fails. Unreachable today (the ``__init__``
  binds nothing), and named here so it is not mistaken for covered.
- Scope is the importing file's package PATH plus the resolved module of the import, so a file moved
  within the package stays covered and a relative ``from .load import _PairedRows`` is caught as the
  absolute spelling is — *on a path that exists on disk*. ``resolved_module`` stats for the package
  root and returns ``None`` otherwise, so against a synthetic filepath the relative spelling fails
  OPEN while the absolute one still fires; a test must use a real path under the package. Routing
  through :func:`tests.lint.import_resolution.resolved_module` is mandatory, not stylistic: CE051 is
  the meta-rule that requires it, and a rule matching ``node.module`` alone would read a relative
  sibling import as the bare module ``"load"`` and fail OPEN everywhere.
- A private import from OUTSIDE this package (``from coder_eval.reports_stats import _betacf``) is
  not this rule's business; ``reports_optimize.py`` is outside the package by construction and
  imports no private name from it.
"""

import ast

from tests.lint.import_resolution import resolved_module
from tests.lint.rules.base import BaseRule


_PACKAGE = "coder_eval.optimize"
_PACKAGE_PATH = "/coder_eval/optimize/"


class NoSiblingPrivateImports(BaseRule):
    """CE059: a cross-module name in ``coder_eval/optimize/`` may not begin with ``_``."""

    id = "CE059"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_package = _PACKAGE_PATH in filepath.replace("\\", "/")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._in_package:
            return
        target = resolved_module(node, self.filepath)
        if target is None or not target.startswith(f"{_PACKAGE}."):
            return
        for alias in node.names:
            if alias.name.startswith("_"):
                self.violation(
                    node,
                    f"imports the private name {alias.name!r} from the sibling module {target!r}. "
                    "Inside coder_eval/optimize/ a name two modules share is PUBLIC — drop the "
                    "underscore. One underscore cannot mark both 'shared within the package' and "
                    "'local to this file', and spelling the first like the second tells a reader "
                    "the signature is safe to change when four modules depend on it.",
                )
        self.generic_visit(node)
