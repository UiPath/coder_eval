"""CE053: a run-tree reader in the optimize family reconciles, or says why not.

**The bug it is written for.** ``run.json`` is a per-INVOCATION artifact while the tree under it
is APPEND-ONLY, so a re-used ``--run-dir`` leaves an earlier call's ``<row>/<NN>/task.json``
behind while ``row_selection`` is rewritten to describe only the latest one. Both gates run
``reconcile_tree_against_run_json`` against exactly that and refuse. Four other readers of the
same trees did not: measured, on dirs ``activation_gate`` correctly refused,
``measure_noise_floor`` returned a floor computed over an extra pooled row and ``arm_row_scores``
returned the stale row in its vector. The floor decides whether a round runs at all and the
vectors feed all three Pareto fronts, so neither number is cosmetic.

**Why a rule rather than four fixes.** The reader set is not closed — the family gains a reader
whenever a new surface wants per-row data — and the failure is SILENT in every case: a
contaminated tree loads, parses and returns a confident number. There is nothing to notice.

**What it detects, precisely.** In the six optimize-gate modules, a function whose body calls
``load_suite_rows`` or ``load_arm_rows`` must also call ``reconcile_tree_against_run_json`` or
``reconcile_arms``, the sweep over a whole ``(variant, run dirs)`` set that wraps it. One
violation per function, anchored at the first offending read.

Two accepted names rather than one because the two are genuinely different grains and both are
in the tree: ``execution_gate`` reconciles one run dir per variant and needs the per-dir result,
while every other reader sweeps a whole arm. Naming the sweep here is the same move CE040 makes
for ``bootstrap_p_floor`` and CE042 for ``replicate_subdir_name`` — the rule points at the single
declaration, so a reader routed through it is satisfied rather than suppressed.

``load_arm_rows`` is exempt BY NAME: it calls ``load_suite_rows`` and is the primitive both
tracks compose from, so a primitive that reconciled would double every gate's work.

The scope is the ``optimize/`` DIRECTORY rather than a list of module names, so the whole family
is covered and a new module cannot silently leave the rule's reach. It replaced an ``optimize_*``
filename prefix when the family became a package, and the two are INCOMPARABLE rather than ordered:
a directory covers whatever is put in it without relying on a naming convention, and is blind to a
reader written at ``src/coder_eval/optimize_new.py`` — which the prefix caught, and which is now
the wrong place to put one. Stated rather than glossed, because this rule's failure mode is silence.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.**

- It matches call NAMES in the same function body. A reader that reaches the tree through a NEW
  helper is invisible to it, and so is a reconcile performed by the caller — which is why the two
  legitimate cases in the tree carry a reasoned ``# noqa: CE053`` rather than a second call.
- It checks that the sweep is CALLED, never that its result is acted on. ``stale, _ =
  reconcile_arms(...)`` with the ``if stale:`` branch dropped passes — which is the likeliest
  future regression, since copying the call is easy and copying the branch is a second step.
  Nothing an AST walk can see distinguishes the two; the behavioural tests in
  ``tests/test_optimize_layering.py::TestStageAReadersReconcileTheTree`` are what cover it.
- It does not check that the reconciliation is over the SAME run dirs and variant the read uses.
  Both are usually the function's own parameters, but a mismatched pair would pass.
- Nested functions are folded into their enclosing top-level one: ``execution_gate`` reconciles in
  its own body and loads there too, and a closure that only reads is not a separate reader.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Matched against the forward-slash-normalized path (the convention CE009 sets), so a backslash
# alternative here would be dead: `filepath.replace` runs first.
#
# The package DIRECTORY, deliberately, not the six-name alternation this was first written as (nor
# the `optimize_*` filename prefix that replaced it). An enumeration fails OPEN on the one change
# most likely to break it: a reader moving into a SEVENTH module reports zero violations, which is
# byte-identical to a clean tree — the CE051 direction. The directory costs nothing to be wrong
# about, since a module here with no tree reader is simply never matched (`optimize/store.py` is one
# today), and it does not depend on a future module being named to a convention.
# Recursive, and the character class is deliberately wide. `[a-z_]+` directly below the package
# would have been two fail-open holes at once: a reader in `optimize/<subpackage>/x.py` and a module
# whose name carries a digit both report ZERO violations, byte-identical to a clean tree. Neither is
# hypothetical enough to gamble on for a rule whose failure mode is silence.
_OPTIMIZE_MODULES = re.compile(r"/coder_eval/optimize/(?:[\w]+/)*[\w]+\.py$")

_TREE_READERS = frozenset({"load_suite_rows", "load_arm_rows"})
# The primitive and the whole-arm sweep that wraps it. See the module docstring for why both.
_RECONCILE = frozenset({"reconcile_tree_against_run_json", "reconcile_arms"})

# The primitive itself: `load_arm_rows` IS a `load_suite_rows` caller, and reconciling inside it
# would run the sweep once per composing gate rather than once per gate.
_EXEMPT = frozenset({"load_arm_rows"})


def _called_name(node: ast.Call) -> str | None:
    """The callee's bare name, through a plain name or an attribute access."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class RunTreeReadersReconcile(BaseRule):
    id = "CE053"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(_OPTIMIZE_MODULES.search(filepath.replace("\\", "/")))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Deliberately NOT `generic_visit`: a nested def is part of its enclosing function's body,
        # so walking the whole subtree here and stopping the traversal is what folds the two.
        if not self._in_scope or node.name in _EXEMPT:
            return
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        reads = [c for c in calls if _called_name(c) in _TREE_READERS]
        if not reads or any(_called_name(c) in _RECONCILE for c in calls):
            return
        # EARLIEST by line, not `ast.walk` order — that is breadth-first, so a read nested in an
        # `if` sorts after a shallower read below it. The anchor is where the suppression comment
        # has to sit, so "the first offending read" has to mean the first one a reader sees.
        self.violation(
            min(reads, key=lambda c: (c.lineno, c.col_offset)),
            f"{node.name}() reads the run-directory tree but never calls {' or '.join(sorted(_RECONCILE))}. "
            "run.json is written per INVOCATION while the tree is APPEND-ONLY, so a re-used "
            "--run-dir leaves an earlier call's results on disk — they load, parse and are pooled "
            "into whatever this returns, silently. Reconcile and refuse (or warn, where the return "
            "type has nowhere to put a refusal), or suppress with a reason saying who reconciles "
            "instead",
        )
