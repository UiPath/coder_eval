"""CE051: an import-matching lint rule must route through ``resolved_module`` — and must not
hand-roll its own ``visit_ImportFrom`` when :meth:`~tests.lint.rules.base.BaseRule.check_import`
exists.

**TWO checks, one id, and neither subsumes the other.** The hook makes the correct path the DEFAULT
path — a rule overriding ``check_import`` receives an already-resolved module and cannot get the
resolution wrong — so the second check is what stops a new rule from opting back out of it. The
first check remains necessary because it is WIDER in two ways the hook cannot reach, both of which
the boundary notes below already record: it scans all of ``tests/``, where the same blindness lives
in ``test_optimize_layering.py``'s ``_coder_eval_imports`` (not a ``BaseRule``, so it can never use
the hook), and it catches shapes that are not a ``visit_ImportFrom`` at all — a lambda, or a
``node.module`` read inside an ``ast.walk``, which exists in the tree today
(``ce020_no_sdk_typed_base_agent_fields.py``). Narrowing CE051 to the rules directory, or to
"defines ``visit_ImportFrom``", would have dropped every one of those.

**The failure class this polices, with four confirmed members.** Five rules under
``tests/lint/rules/`` match on an import's module string, and every one of them read
``node.module`` while none read ``node.level``. For a relative import — ``from ..models import X``
— ``node.module`` is ``"models"``, so a pattern anchored on ``coder_eval.models`` does not match
and the rule reports nothing. Measured by feeding each rule the absolute and the relative spelling
of one violation: CE001, CE004, CE017 and CE023 fired on the first and were silent on the second,
and CE041 reported **0 violations against 8 real model-constructor splats in ``src/``** — it had
never fired in its life.

What makes that worth a rule of its own rather than five fixes is the DIRECTION of the failure. A
broken import rule fails **OPEN**: it reports zero violations, which is byte-identical to a clean
codebase. Nothing goes red, nothing looks wrong, and ``make lint`` keeps printing a clean bill of
health — which is how four rules stayed broken simultaneously, one of them for its entire life.

**Why it keys on the resolver call rather than on ``node.level``.** "Mentions ``node.level``" is
satisfiable by a stray reference and says nothing about whether the resolution is right. "Calls
``resolved_module``" is the DRY rule and the correctness rule at once: there is one resolver, it is
unit-tested, and a rule routed through it cannot be half-right.

**Boundary, stated so a green ``make lint`` is not mistaken for a proof.**

* It pins the resolver CALL, not that the comparison downstream is correct. A rule that resolves
  the module and then matches the wrong pattern is out of its reach.
* A rule matching on the imported NAMES (``node.names``) rather than the module is out of scope —
  it has no module string to get wrong.
* It reports a GAP rather than passing vacuously: if no rule file defines ``visit_ImportFrom`` at
  all, that is a renamed or restructured harness and is flagged, mirroring CE044's treatment of a
  renamed ``_ALLOWED_OPS``.
* Lambdas are collected alongside ``def``s: a matcher written as
  ``visit_ImportFrom = lambda self, node: … node.module …`` used to drop out of scope entirely,
  and because the reader set was then empty the anti-vacuity GAP did not fire either — a silent
  double miss in a rule whose whole job is preventing silent misses.
* Still NOT matched, and stated rather than left to be found: ``getattr(node, "module", "")``,
  and a rule that calls the resolver and then discards its result in favour of ``node.module``.
  The second is the shape a hasty fix takes.
* It applies only to a rule that matches a FIRST-PARTY module — one whose own non-docstring
  string constants mention ``coder_eval``. A rule matching a third-party package (CE020 matches
  ``claude_agent_sdk``) is exempt by construction rather than by a list, because a relative import
  can never resolve to a package outside the importing one: there is no blindness to fix there.
* The module that DEFINES ``resolved_module`` is exempt — it reads ``node.module`` because it
  is the thing every other rule routes through, and it cannot call itself. Keyed on the
  functions it defines rather than on its path.
* Scope is decided by the rule's own string CONSTANTS, so a rule that builds its pattern from an
  f-string, or imports it from another module, has no first-party constant of its own and drops
  out of scope entirely. Both are real evasion routes and neither is detected.
* The SECOND check is scoped to ``tests/lint/rules/`` and exempts ``base.py``, which is the one
  file that must define ``visit_ImportFrom``. It is anchored against vacuity on ``base.py`` itself:
  checking that file asserts it defines BOTH ``visit_ImportFrom`` and ``check_import``, so renaming
  or deleting the hook is reported there rather than quietly making the whole check a no-op
  (CE044's lesson). It says nothing about whether an overriding ``check_import`` is CORRECT.
* It scans all of ``tests/`` — not just ``tests/lint/rules/``. The class is not confined to
  numbered rules: ``tests/test_optimize_layering.py``'s ``_coder_eval_imports`` had exactly the
  same blindness, and it is the helper behind the ``optimize/`` / ``reports_optimize``
  layering pin CLAUDE.md calls out as held "by a test, not by this sentence". Scoping to the
  rules package would have guaranteed nothing ever caught it.
"""

import ast

from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_TEST_TREE = "tests/"
_RULES_DIR = "tests/lint/rules/"
_BASE_MODULE = "tests/lint/rules/base.py"
_RESOLVER = "resolved_module"
_RESOLVER_MODULE_MARKER = "_package_chain"
_VISITOR = "visit_ImportFrom"
_HOOK = "check_import"

_FIX = (
    "this `visit_ImportFrom` reads `node.module` without routing through "
    "`tests.lint.import_resolution.resolved_module`. For a relative import (`from ..models import "
    "X`) `node.module` is just `'models'`, so the match silently fails and the rule reports ZERO "
    "violations — indistinguishable from a clean tree. Four rules were broken this way at once. "
    "Call `resolved_module(node, self.filepath)` and match on its result."
)

_GAP = (
    "no `visit_ImportFrom` method found in this rule file, but it reads `node.module` — CE051 "
    "cannot verify the resolver is used. If the rule was restructured, teach CE051 the new shape "
    "rather than leaving it to pass vacuously."
)

_HAND_ROLLED = (
    "this rule defines its own `visit_ImportFrom`. Override `BaseRule.check_import(node, module)` "
    "instead: it hands you the ABSOLUTE module with `node.level` already resolved, which makes the "
    "correct path the default one. A rule that resolves for itself can forget to — five did at "
    "once, and the failure is silent, because an import rule that never matches reports zero "
    "violations exactly like a clean tree."
)

_HOOK_GAP = (
    f"`BaseRule` no longer defines both `{_VISITOR}` and `{_HOOK}`, so CE051's second check has "
    "nothing to route rules TO and would pass vacuously on every rule file. If the hook was "
    "renamed, rename it here too."
)


def _reads_node_module(node: ast.AST) -> bool:
    """True when the subtree reads the ``.module`` attribute off anything.

    Attribute name only, deliberately: the receiver is ``node`` by convention in every rule here,
    and pinning the receiver's name would make the check evadable by renaming one parameter.
    """
    return any(isinstance(n, ast.Attribute) and n.attr == "module" for n in ast.walk(node))


def _calls_resolver(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == _RESOLVER)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == _RESOLVER)
        )
        for n in ast.walk(node)
    )


def _matches_first_party(tree: ast.AST) -> bool:
    """True when this rule's own patterns name ``coder_eval``.

    Docstrings are excluded by NODE IDENTITY, not by comparing text. ``ast.get_docstring`` returns
    the ``inspect.cleandoc``-normalised string, which never equals the raw ``ast.Constant.value``
    of a multi-line docstring (de-indented, trailing newline) — so a ``value not in docstrings``
    test excludes nothing at all and every rule whose PROSE mentions ``coder_eval`` is dragged into
    scope. Measured: adding the phrase to CE020's module docstring and changing nothing else
    produced two false violations against a rule this function is supposed to exempt.

    What is left after the exclusion is the constants a rule actually matches against — CE001's
    submodule regex, CE041's ``_MODELS_MODULE`` — versus CE020's ``claude_agent_sdk``.
    """
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstring_nodes.add(id(first.value))
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstring_nodes
        and "coder_eval" in n.value
        for n in ast.walk(tree)
    )


class ImportFromRulesHandleLevel(BaseRule):
    id = "CE051"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_tests = _TEST_TREE in filepath.replace("\\", "/")

    def check(self, tree: ast.AST) -> list[Violation]:
        """Analysed whole-tree rather than through visitor methods.

        Scope depends on the file's CONSTANTS (first-party or not), which is not knowable when
        ``__init__`` runs with only a path — and the vacuity guard needs to know whether any
        ``visit_ImportFrom`` was seen at all, which is a fact about the whole file.
        """
        if not self._in_tests:
            return []

        # Checked BEFORE the first-party scope test, and deliberately: a rule that hand-rolls
        # `visit_ImportFrom` is opting out of the resolved-module hook whatever it happens to match
        # on, so gating this on the rule's own constants would exempt exactly the rules that have
        # not started matching a first-party module YET.
        self._check_hook_is_used(tree)

        if not _matches_first_party(tree):
            return self.violations
        # The resolver itself reads `node.module` and cannot call itself. Detected by what it
        # DEFINES rather than by its path, so moving the file does not silently re-exempt or
        # re-flag it.
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        if _RESOLVER in defined and _RESOLVER_MODULE_MARKER in defined:
            return self.violations

        functions: list[ast.AST] = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        ]
        visitors = [
            f
            for f in functions
            if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef) and f.name == "visit_ImportFrom"
        ]
        # A helper like CE041's `_imports_models` does the matching on the visitor's behalf, so it
        # is where the violation belongs — reporting the caller would point at the wrong line.
        readers = [f for f in functions if _reads_node_module(f)]

        for func in readers:
            if not _calls_resolver(func):
                self.violation(func, _FIX)
        if readers and not visitors:
            # Anchored on the first statement, never on the Module node: a Module has no `lineno`,
            # so `violation()` records line 0 and the runner's noqa lookup — which matches a
            # suppression comment on a line the node SPANS — can never reach it. An unsuppressible
            # violation is not a guard, it is a wall.
            anchor = getattr(tree, "body", None)
            self.violation(anchor[0] if anchor else tree, _GAP)
        return self.violations

    def _check_hook_is_used(self, tree: ast.AST) -> None:
        """No rule under ``tests/lint/rules/`` may define ``visit_ImportFrom``. ``base.py`` must.

        Both halves matter. Without the first, the hook is available and ignorable. Without the
        second — the anti-vacuity anchor — renaming ``check_import`` would leave a check that
        forbids the only shape there is, and every rule file would pass because none of them
        defines the visitor any more.
        """
        path = self.filepath.replace("\\", "/")
        if _RULES_DIR not in path:
            return
        methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        if path.endswith(_BASE_MODULE):
            if not {_VISITOR, _HOOK} <= methods:
                anchor = getattr(tree, "body", None)
                self.violation(anchor[0] if anchor else tree, _HOOK_GAP)
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _VISITOR:
                self.violation(node, _HAND_ROLLED)
