"""The optimize family's CROSS-MODULE contracts — the claims no single module can make.

The rank ladder and its coverage check, the anti-facade and no-flat-shim sensors, the store edge, the
no-CLI-machinery scan, and every "both tracks agree" assertion. A class lands here when its subject
spans two modules: `activation -> execution` is acyclic and still wrong, and only a test that sees
both can say so.

`_OPTIMIZE_RANKS` below is the ladder's ONE declaration. It is a SPECIFICATION, not a derivation —
see the comment above it before proposing to compute it.
"""

import ast
import inspect
import json
import logging
import math
import pkgutil
import re
import shutil
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest
from pydantic import ValidationError

import coder_eval.optimize
from coder_eval.models import (
    ActivationGateVerdict,
    ConfirmVerdict,
    ExecutionGateVerdict,
    GateVerdictBase,
    GuardrailCheck,
    copy_with,
)
from coder_eval.models.optimize import _FIELD_OVERRIDES
from coder_eval.optimize.activation import activation_gate, confirm_gate, holm_promote, measure_noise_floor
from coder_eval.optimize.execution import holm_promote_execution, measure_execution_noise_floor
from coder_eval.optimize.fronts import arm_row_scores, cost_quality_points
from coder_eval.optimize.gate import (
    GATE_MAX_FAMILY,
    GATE_P_PRECISION,
    GATE_RESAMPLES,
    NOTE_OUTSIDE_FAMILY,
    build_confirm_verdict,
    classify_confirm,
    confirm_split_check,
    decide_family,
    holm_family,
    note_holm_family,
    note_ordinary_negative,
)
from coder_eval.optimize.load import TASK_JSON_GLOB, SplitProvenance, wrong_path_reason
from coder_eval.optimize.store import UNRECORDED_SPLIT
from coder_eval.reports_optimize import render_execution_markdown, render_markdown
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES, DEFAULT_ALPHA, holm_rejections
from tests.lint.import_resolution import resolved_module
from tests.optimize_fixtures import (
    EXEC_SUITE,
    FAST_RESAMPLES,
    REFUSAL_RESAMPLES,
    SUITE,
    WINNER,
    activation_verdict,
    activation_verdict_over_arms,
    confirm_activation_arms,
    eval_result,
    exec_gate,
    exec_run_dir,
    execution_floor,
    expected_resolution_note,
    failing_cost_check,
    full_activation_verdict,
    full_execution_verdict,
    full_guardrail_check,
    headline_line,
    module_path,
    module_source,
    parity_activation,
    parity_execution,
    pinned_suite,
    scored_result,
    shared_dirs,
    tiny_suite,
    uniform_shift,
    weighted_arm,
    write_arm,
    write_row,
)


class TestEveryWrongPathMessageDerivesFromTheGlob:
    """The four messages that tell a reader what did not match are built from `TASK_JSON_GLOB`.

    They used to spell the pattern as a literal `*/NN/task.json`, in four places, none of which
    was the glob the loader actually ran. Changing the glob left all four describing a tree the
    code no longer searched — and these are the messages a reader consults precisely when the
    path is what is wrong.
    """

    def test_the_activation_zero_row_note_derives(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="typo",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )
        note = next(n for n in verdict.notes if "loaded ZERO rows" in n)
        assert TASK_JSON_GLOB in note

    def test_the_execution_zero_row_refusal_derives(self, tmp_path: Path) -> None:
        # Spelled with the literal `<variant>` because this one message names BOTH arms.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = exec_gate(run_dir)
        assert verdict.gate_refusal is not None
        assert f"<run>/<variant>/{EXEC_SUITE}/{TASK_JSON_GLOB}" in verdict.gate_refusal

    def test_the_activation_floor_reason_derives(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(run_dirs=run_dirs, variant_id="typo", suite_id=SUITE, criterion_index=0, model="m")
                is None
            )
        assert TASK_JSON_GLOB in caplog.text

    def test_the_execution_floor_reason_derives(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with caplog.at_level(logging.WARNING):
            assert (
                measure_execution_noise_floor(run_dirs=run_dirs, variant_id="typo", suite_id=SUITE, model="m") is None
            )
        assert TASK_JSON_GLOB in caplog.text

    def test_no_message_spells_the_padding_itself(self) -> None:
        # The whole point of the seam: a message may name the glob, never the padding it replaced.
        # Over the WHOLE family: every message that names the glob moved out of `optimize/gate.py`
        # in the split, which left this scan reading a file with none of its subjects in it.
        assert "*/NN/task.json" not in _family_source()
        assert TASK_JSON_GLOB == "*/*/task.json"


def _family_source() -> str:
    """Every decision-family module's source, concatenated.

    Scans that read `optimize/gate.py` alone went VACUOUS the moment the split moved their
    subjects: proven by mutation, an inlined `min(len(a), len(b))` and a respelled shared note both
    passed. A whole-family read is what keeps them asserting something.

    Derived from `_PACKAGE_MODULES`, not from `_OPTIMIZE_RANKS`: the ranks exclude the ladder-exempt
    `store`, which left it outside every scan built on this while the sentence above claimed "every
    module". `store` is exempt from the LADDER, not from "declared once".
    """
    return "\n".join(module_source(module) for module in sorted(_PACKAGE_MODULES))


def test_the_trim_is_declared_once() -> None:
    """`min(len(a), len(b))` may appear in exactly one function: `balance_pair`.

    Counts call NODES, mirroring `TestHolmRejectionsIsConfined`. The trim was spelled three times
    in three shapes and only one of the three surfaced the dropped observations to the user, which
    is precisely how a re-weighted comparison stays invisible.

    `measure_execution_noise_floor`'s `min(len(values) for values in replicated)` is explicitly
    allowed: a GENERATOR argument rather than two `len` arguments, and a minimum ACROSS rows rather
    than between two arms of one row — a different computation that stays separate.
    """
    tree = ast.parse(_family_source())

    offenders: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "min"):
                continue
            two_lens = len(node.args) == 2 and all(
                isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "len" for a in node.args
            )
            if two_lens and function.name != "balance_pair":
                offenders.append(f"{function.name}:{node.lineno}")
    assert not offenders, f"a per-arm observation trim outside balance_pair: {offenders}"


class TestBothTracksEmitTheSameHolmNotes:
    """The four notes both wrappers emit are one declaration, not two byte-identical copies.

    They lived 600 lines apart, so a wording fix applied to one would have left the two tracks
    describing the same decision differently in a ledger read back weeks later.
    """

    def test_the_ordinary_negative_note_is_identical_across_tracks(self, tmp_path: Path) -> None:
        # Same p, same family size, same alpha on both tracks — so any difference in the produced
        # string is a difference in the DECLARATION, which is what this pins.
        activation = note_ordinary_negative(0.4321, 3, DEFAULT_ALPHA)
        assert activation == note_ordinary_negative(0.4321, 3, DEFAULT_ALPHA)
        assert "0.4321" in activation and "family of 3" in activation

    def test_both_wrappers_emit_the_shared_declarations(self, tmp_path: Path) -> None:
        # Through the real wrappers, not the constants: a call site that kept its own copy would
        # still produce an equal string today, so the source scan below is what makes this tight.
        incumbent = {f"r{i}": [("yes", "no" if i else "yes")] for i in range(6)}
        activation = holm_promote([activation_verdict(shared_dirs(tmp_path, incumbent, incumbent))])[0]
        execution = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **WINNER))])[0]
        for verdict in (activation, execution):
            assert note_holm_family(1, DEFAULT_ALPHA) in verdict.notes

    def test_neither_wrapper_respells_a_shared_note(self) -> None:
        # Over the WHOLE family: BOTH wrappers left `optimize/gate.py` in the split, so a scan of
        # that file alone counts zero of everything and passes on a respelled note.
        source = _family_source()
        for fragment in (
            "the sample could not support a p-value",
            "the Holm-corrected test rejects but the confidence",
            "did not clear the Holm threshold for its rank",
            "Holm applied across a family of",
        ):
            assert source.count(fragment) == 1, f"{fragment!r} is declared more than once"


class TestTheVerdictCopySeamWritesOnlyDeclaredFields:
    """Both wrappers write their decision through `copy_with`, whose keys pydantic never validated.

    `model_copy(update=)` sets an unknown key as a bare instance attribute: absent from
    `model_dump()`, with the field it was meant to set left at its default and nothing raised. On
    these two models — the ones that say what a promotion decision rests on — a `mean_dif` typo
    renders a block reporting no difference at all.

    So a decided verdict's INSTANCE STATE is pinned, not only its values: no attribute outside the
    declared fields, and the fields the copy writes carrying what the wrapper decided.

    Be precise about what that catches. `copy_with` raises on a bad key, so while these wrappers go
    through it the stray-attribute check cannot fire — and a call site reverted to
    `model_copy(update=)` with CORRECT keys does not trip it either (CE048 is what catches that).
    It fires on the conjunction: a reverted call site AND a wrong key, which is the state that
    ships a verdict silently reporting no difference. The value assertions below are what carry
    these tests day to day.

    Read `__dict__`, never `model_dump()`. Under `extra="forbid"` an undeclared key set by
    `model_copy(update=)` lands in `__dict__` and is never serialized, so
    `set(model_dump()) == set(model_fields)` is true **even when the defect is present** — and
    `model_extra` is `None`, because that is only populated under `extra="allow"`. A sensor built
    on either would assert something that cannot be false, which is the CE039 failure mode this
    repo already has a rule class for.
    """

    @staticmethod
    def _assert_no_attribute_outside_the_model(verdict) -> None:
        stray = sorted(set(verdict.__dict__) - set(type(verdict).model_fields))
        assert not stray, (
            f"{stray} sit on the verdict as bare instance attributes. That is what an unvalidated "
            "model_copy(update=) does with a mistyped key: the field it was meant to set is left "
            "at its default, and model_dump() never mentions either."
        )

    def test_the_assertion_bites_on_the_defect_it_describes(self, tmp_path: Path) -> None:
        """Anti-vacuity. The first draft of this class asserted on `model_dump()` and could not fail.

        Built by doing to a real verdict exactly what a mistyped `model_copy(update=)` would.
        """
        lone = {"r0": [("yes", "yes")]}
        verdict = activation_verdict(shared_dirs(tmp_path, lone, lone))
        typo = verdict.model_copy(update={"promotd": True})  # CE048 scans src/ only; this IS the defect

        assert set(typo.model_dump()) == set(type(typo).model_fields), (
            "the model_dump() check the first draft used still passes here — that is why this "
            "class reads __dict__ instead"
        )
        with pytest.raises(AssertionError, match="promotd"):
            self._assert_no_attribute_outside_the_model(typo)

    def test_a_promoted_activation_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        decided = holm_promote([activation_verdict(shared_dirs(tmp_path, incumbent, candidate))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.promoted is True
        assert decided.holm_alpha == DEFAULT_ALPHA
        assert decided.gate_refusal is None
        assert decided.notes and note_holm_family(1, DEFAULT_ALPHA) in decided.notes

    def test_an_unfamilied_activation_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        # The `p_value is None` branch, which writes three fields rather than four.
        lone = {"r0": [("yes", "yes")]}
        decided = holm_promote([activation_verdict(shared_dirs(tmp_path, lone, lone))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.p_value is None
        assert decided.promoted is False
        assert decided.holm_alpha == DEFAULT_ALPHA

    def test_a_promoted_execution_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **WINNER))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.promoted is True
        assert decided.holm_alpha == DEFAULT_ALPHA
        assert decided.mean_diff is not None and decided.mean_diff > 0.0

    def test_a_refused_execution_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        one_row = {"incumbent": {"r1": [0.2, 0.3]}, "candidate": {"r1": [0.7, 0.8]}}
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **one_row))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.gate_refusal is not None
        assert decided.promoted is False


# The package's own contents, DERIVED — a new module joins every layering test at once, without
# anyone remembering to extend a tuple. Names are dotted and package-relative to `coder_eval`
# (`module_path`'s docstring says why). `iter_modules` does not yield `__init__` but it DOES yield
# a private module, so the leading-underscore filter is load-bearing rather than defensive: a future
# `_helpers.py` is a file-local detail, not a family member with a rank.
_PACKAGE_MODULES = {
    f"optimize.{name}" for _, name, _ in pkgutil.iter_modules(coder_eval.optimize.__path__) if not name.startswith("_")
}
# The whole optimize family plus the one sibling that was never part of the split. `reports_optimize`
# is named separately because it belongs to the REPORTS family by design, and the assertion that no
# family module imports it is precisely that boundary. Every layering test iterates this.
_OPTIMIZE_MODULES = (*sorted(_PACKAGE_MODULES), "reports_optimize")
# The DECISION layer's six modules, at their ranks. An import may only point at a STRICTLY lower
# rank: `activation -> execution` is acyclic and still wrong, because the two tracks are meant to
# be independently readable.
#
# DECLARED, never derived, and a future author will reasonably propose deriving it — do not. Compute
# each rank as its longest path in the import DAG and `activation -> execution` simply makes
# `execution` rank 2 and `activation` rank 3: the assert then passes over the exact violation it
# exists to catch. This map IS the specification; `pkgutil` supplies only the COVERAGE check below.
_OPTIMIZE_RANKS = {
    "optimize.load": 0,
    "optimize.gate": 1,
    "optimize.activation": 2,
    "optimize.execution": 2,
    "optimize.fronts": 3,
    "optimize.search": 3,
}
# `store` is the sidecar every rank imports and is deliberately outside the ladder — the ONE
# documented exception, for the same reason `reports_optimize` is the one tail above.
_LADDER_EXEMPT = frozenset({"optimize.store"})
# The word "ONE" in `__init__.py` and CLAUDE.md, asserted. A second exemption would slip past the
# rank check, the store-edge assertion AND `test_the_store_module_imports_only_models` — all three
# name `store` specifically — so the count is the only thing standing in its way.
assert len(_LADDER_EXEMPT) == 1, f"a second ladder exemption needs its own reason and its own checks: {_LADDER_EXEMPT}"


def _rank_coverage_gap(package_modules: set[str]) -> tuple[list[str], list[str]]:
    """(unranked package modules, ranked names the package no longer has).

    A FUNCTION rather than an inline comparison so the module-scope assert below and the test that
    proves it can fail run the same code. Inlining it twice is how a coverage check ends up with a
    test that re-implements the comparison and therefore cannot fail — measured on the first draft
    of exactly this pair.
    """
    ranked = set(_OPTIMIZE_RANKS)
    return sorted(package_modules - _LADDER_EXEMPT - ranked), sorted(ranked - package_modules)


def _coder_eval_imports(module: str, *, inside_type_checking: bool | None = None) -> dict[str, set[str]]:
    """`coder_eval` imports in a module, keyed by module path.

    AST-based, not a substring scan: these modules are heavily docstringed and a raw-text check
    over them is exactly the fragile presence sensor CE039 exists to discourage.

    ``inside_type_checking`` filters to imports inside (True) or outside (False) an
    ``if TYPE_CHECKING:`` body; ``None`` takes both.

    Routed through ``resolved_module`` (CE051). Matching ``node.module`` alone made this blind to
    the RELATIVE spelling, which is how most of ``src/`` imports — and these three modules are
    siblings, so ``from .optimize.gate import CostQualityPoint`` in ``reports_optimize.py`` is the
    natural way to write the very import this pins. ``node.module`` would then be
    ``"optimize.gate"``, the ``startswith("coder_eval")`` test would be False, and both layering
    tests would pass over a broken boundary — the same fail-open shape, on what CLAUDE.md calls
    out as pinned "by a test, not by this sentence".
    """
    path = str(module_path(module).resolve())
    tree = ast.parse(module_source(module))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            guarded |= {id(child) for stmt in node.body for child in ast.walk(stmt)}

    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = resolved_module(node, path)
        if not (resolved or "").startswith("coder_eval"):
            continue
        if inside_type_checking is not None and (id(node) in guarded) != inside_type_checking:
            continue
        found.setdefault(resolved or "", set()).update(alias.name for alias in node.names)
    return found


# The COVERAGE check on the ladder above, at MODULE scope so it fails COLLECTION rather than one
# test: a new module in `coder_eval/optimize/` with no rank stops this file dead instead of quietly
# skipping whichever layering test forgot it. It also catches the reverse — a rank naming a module
# that is gone — which is what makes an EMPTY derivation fail loudly rather than pass over nothing.
#
# It was silently DROPPED when this file was split out of the monolith: the splitter moved statements
# that define a NAME, and a bare `assert` defines none. Nothing failed, because an assert is not a
# test and the node-id parity check that guarded the split cannot see one.
assert _rank_coverage_gap(_PACKAGE_MODULES) == ([], []), (
    f"the ranks and coder_eval/optimize/ disagree: {_rank_coverage_gap(_PACKAGE_MODULES)}. Give a "
    "new module a rank in _OPTIMIZE_RANKS, or add it to _LADDER_EXEMPT with the reason, as "
    "`store` is"
)
# `store` is the one name the coverage check cannot see either way, so it is asserted separately: a
# derivation that lost only the sidecar would otherwise satisfy the check above untouched.
assert _LADDER_EXEMPT <= _PACKAGE_MODULES, f"the ladder-exempt sidecar is missing: {sorted(_LADDER_EXEMPT)}"


def test_the_presentation_module_makes_no_decisions_and_reads_no_disk() -> None:
    """What makes the split real rather than cosmetic.

    A renderer that reaches back for a run directory or an estimator is a gate with a table on it,
    and the module boundary then documents a separation that does not exist. The NamedTuples it needs
    — four of them now, across three modules — are allowed only under `if TYPE_CHECKING`, so the
    runtime dependency on the decision layer is exactly zero.
    """
    runtime = _coder_eval_imports("reports_optimize", inside_type_checking=False)
    assert set(runtime) <= {"coder_eval.models", "coder_eval.reports_stats"}, (
        f"reports_optimize gained a runtime coder_eval dependency: {sorted(set(runtime))}"
    )
    # One display value, and CE040 requires it be DERIVED there rather than respelled.
    assert runtime.get("coder_eval.reports_stats", set()) == {"bootstrap_p_floor"}

    deferred = _coder_eval_imports("reports_optimize", inside_type_checking=True)
    # THREE modules now, and the set is asserted whole rather than per-module: what matters is that
    # these are the ONLY names it defers, wherever they now live. Every one is a NamedTuple —
    # computed and rendered, never persisted — which is exactly the category that stays with its
    # producer and is deferred here. A MODEL would be imported at runtime from `coder_eval.models`
    # instead, so a new name appearing in this dict is a signal to check which category it is.
    assert deferred == {
        "coder_eval.optimize.activation": {"SeedStability"},
        "coder_eval.optimize.fronts": {"CostQualityPoint", "RuleCeiling"},
        "coder_eval.optimize.search": {"SearchComparison"},
    }, deferred

    # No filesystem call anywhere in the module.
    tree = ast.parse(module_source("reports_optimize"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    filesystem = {"open", "read_text", "write_text", "glob", "rglob", "mkdir", "replace", "Path"}
    assert not called & filesystem, f"reports_optimize touches the filesystem: {sorted(called & filesystem)}"


def test_the_gate_never_imports_its_presentation() -> None:
    # The mirror direction. If anything drives you to add this edge, the split boundary is wrong.
    assert "coder_eval.reports_optimize" not in _coder_eval_imports("optimize.gate")


def test_the_store_module_imports_only_models() -> None:
    # `optimize.gate` -> `optimize.store` is the only edge between the three non-model modules, so
    # anything else here would close a cycle.
    assert set(_coder_eval_imports("optimize.store")) <= {"coder_eval.models"}


def test_a_moved_name_lives_in_exactly_one_module() -> None:
    """No name one family module defines survives on another except a deliberate re-import.

    **This is the split's contract**, and it is what forbids a re-export facade: a module that
    kept a copy of a moved name would let every old import keep working and make the split
    cosmetic. Widened from two modules to the whole family rather than duplicated — the loop was
    already derived on both sides, so growing it covered five new modules for free.

    DERIVED on both sides, never a hand-written list: each module's contents come from enumerating
    it (with a `__module__` filter, so a name it merely IMPORTS does not count as one it defines),
    and the permitted survivors come from parsing the OTHER module's own import statements.
    `ruff` keeps that permitted set minimal for free — an unused back-import is F401.
    """
    import importlib

    modules = {name: importlib.import_module(f"coder_eval.{name}") for name in _OPTIMIZE_MODULES}
    imports = {name: _coder_eval_imports(name) for name in _OPTIMIZE_MODULES}

    for owner, module in modules.items():
        defined = [
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        ]
        assert defined, f"{owner} defines nothing — the enumeration is not doing its job"
        for other, other_module in modules.items():
            if other == owner:
                continue
            back_imported = imports[other].get(f"coder_eval.{owner}", set())
            leftovers = [n for n in defined if hasattr(other_module, n) and n not in back_imported]
            assert not leftovers, f"{owner} names still defined on {other}: {leftovers}"


def test_the_optimize_package_reexports_nothing() -> None:
    """No facade, and it is machine-checked rather than remembered.

    A package `__init__` re-exporting the family would let every pre-package import keep working
    and make the split cosmetic — exactly the property
    `test_a_moved_name_lives_in_exactly_one_module` forbids one module over. A submodule bound by
    an importing test is fine; a function, class or constant is not.
    """
    bound = {
        name: value
        for name, value in vars(coder_eval.optimize).items()
        if not name.startswith("__") and not isinstance(value, ModuleType)
    }
    assert not bound, (
        f"coder_eval/optimize/__init__.py binds {sorted(bound)} — the package re-exports NOTHING "
        "on purpose, so that no pre-package import path can keep working"
    )


def test_no_flat_shim_module_survives_the_move() -> None:
    """The other half of "no facade": a leftover `optimize_load.py` re-exporting the new home.

    Not the same guarantee as `test_the_optimize_package_reexports_nothing` — a package `__init__`
    cannot make `coder_eval.optimize_load` importable, so only a SHIM at the old path can, and only
    this test would see it. Both are needed: the facade property and the old-path property.
    """
    import importlib

    for name in _PACKAGE_MODULES:
        flat = "coder_eval." + name.replace(".", "_")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(flat)


def test_the_rank_coverage_check_fails_on_an_unranked_module() -> None:
    """The module-scope coverage assert, exercised through the same predicate it uses.

    Without this nobody knows the assert can fail. Both directions, because both are silent: a new
    module joining `coder_eval/optimize/` unranked, and a rank left behind naming a module that no
    longer exists — the second is what makes an EMPTY derivation fail loudly rather than pass over
    nothing.
    """
    assert _rank_coverage_gap(_PACKAGE_MODULES) == ([], [])

    unranked, _ = _rank_coverage_gap(_PACKAGE_MODULES | {"optimize.newcomer"})
    assert unranked == ["optimize.newcomer"]

    _, orphaned = _rank_coverage_gap(set())
    assert orphaned == sorted(_OPTIMIZE_RANKS), "an empty derivation must report every rank orphaned"


def test_the_derived_package_module_set_is_the_family_and_every_file_exists() -> None:
    """Non-vacuity, and the ONE place the family is spelled out rather than derived.

    The rank map plus `_LADDER_EXEMPT` is the specification; this asserts the derivation agrees with
    it and that every name resolves to a real file — so a `__path__` that silently found the wrong
    directory cannot read as a clean tree.
    """
    assert set(_OPTIMIZE_RANKS) | set(_LADDER_EXEMPT) == _PACKAGE_MODULES, sorted(_PACKAGE_MODULES)
    assert len(_PACKAGE_MODULES) == 7, sorted(_PACKAGE_MODULES)
    for name in _PACKAGE_MODULES:
        assert module_path(name).is_file(), name


def test_the_import_graph_respects_the_rank_order() -> None:
    """Strictly downward, never sideways — and acyclicity alone would not say that.

    `optimize.activation -> optimize.execution` is perfectly acyclic and still wrong: the two
    tracks are meant to be independently readable, which is the whole reason the split is BY TRACK.
    So the assertion is on the RANKS, not on the absence of cycles.
    """
    for module, rank in _OPTIMIZE_RANKS.items():
        for imported in _coder_eval_imports(module):
            target = imported.removeprefix("coder_eval.")
            if target not in _OPTIMIZE_RANKS:
                continue
            assert _OPTIMIZE_RANKS[target] < rank, (
                f"{module} (rank {rank}) imports {target} (rank {_OPTIMIZE_RANKS[target]}) — "
                "an import must point at a STRICTLY lower rank"
            )


def test_the_store_edge_is_sentinels_and_one_lookup() -> None:
    """The family -> store edge, which is the only one out of the decision layer.

    `UNRESOLVED_MODEL` and `UNRECORDED_SPLIT` live in the store rather than in the gate because
    they are cache-key sentinels the STORE refuses to write — declaring them here would make the
    store import the family and close a cycle.
    """
    edge = {module: _coder_eval_imports(module).get("coder_eval.optimize.store", set()) for module in _OPTIMIZE_RANKS}
    assert edge == {
        "optimize.load": {"UNRECORDED_SPLIT"},
        "optimize.gate": {"UNRESOLVED_MODEL", "lookup_noise_floor"},
        "optimize.activation": {"UNRESOLVED_MODEL"},
        "optimize.execution": {"UNRESOLVED_MODEL"},
        "optimize.fronts": set(),
        "optimize.search": set(),
    }, edge
    # And nothing in the family imports the RENDERER: a decision layer depending on its own
    # presentation is what would make that split cosmetic too.
    for module in _OPTIMIZE_RANKS:
        assert "coder_eval.reports_optimize" not in _coder_eval_imports(module), module


def test_module_imports_no_cli_machinery() -> None:
    """A core library the skill drives from a snippet — not a command.

    CE004 does NOT cover this: its _CORE_DIRS regex matches only the models/criteria/... SUBDIRS,
    so a top-level module is out of scope, and it bans importing `coder_eval.cli` rather than
    typer/rich at all. Hence a real assertion here.
    """
    # All THREE modules: the claim is about the whole surface the skill's snippets import, and the
    # gate's presentation and sidecar halves are exactly as reachable from a snippet as it is.
    for module in _OPTIMIZE_MODULES:
        source = module_source(module)
        for banned in ("import typer", "import rich", "from typer", "from rich", "coder_eval.cli"):
            assert banned not in source, f"{module} imports {banned!r} — it is a library, not a CLI surface"


# The two tracks' (gate, verdict factory) pairs, so a parity claim is asserted over both rather
# than written twice and allowed to drift — which is the defect this whole phase closes.
_TRACKS = [
    pytest.param(holm_promote, parity_activation, id="activation"),
    pytest.param(holm_promote_execution, parity_execution, id="execution"),
]


class TestBothTracksMeanTheSameThingByPromoted:
    """`promoted` is ONE contract now, asserted over both tracks rather than stated twice.

    The activation track used to fold its `sibling_checks` into `promoted` while leaving the
    cost/latency `guardrails` advisory for the skill's prose to gate — so a candidate that
    materially raised what a row costs read `promoted=True`, and a caller reading the field could
    ship what the rendered block called BLOCKED. The execution track already vetoed. These are the
    sensors that stop the two drifting apart again.
    """

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_a_failing_guardrail_forces_promoted_false(self, gate, build) -> None:
        decided = gate([build(guardrails=[failing_cost_check()])])[0]
        assert decided.holm_rejected is True, "Holm did reject it"
        assert decided.separated is True, "and the statistic did separate"
        assert decided.promoted is False, "so the guardrail is what vetoed — on BOTH tracks"

    @pytest.mark.parametrize(
        ("gate", "build", "render", "vetoing"),
        [
            # The list that is NOT the cost/latency guardrails on each track — the one whose
            # failure vetoes without being a "guardrail" by name.
            pytest.param(holm_promote, parity_activation, render_markdown, "sibling_checks", id="activation"),
            pytest.param(
                holm_promote_execution,
                parity_execution,
                render_execution_markdown,
                "integrity_checks",
                id="execution",
            ),
        ],
    )
    def test_a_candidate_vetoed_by_the_non_guardrail_list_still_headlines_blocked(
        self, gate, build, render, vetoing
    ) -> None:
        """Both veto lists must reach the headline, not just the one called "guardrails".

        The last place the two tracks disagreed about what `promoted` means. `render_markdown`
        read `verdict.guardrails` alone while its twin unioned `integrity_checks`, so a candidate
        that separated, cleared Holm and was vetoed by a failing SIBLING check rendered
        `NOT PROMOTED` — indistinguishable from one that simply lost. "It won and was vetoed" and
        "it lost" call for opposite next actions, which is the whole reason this rung exists.
        """
        failing = GuardrailCheck(
            name="the check that vetoed",
            incumbent=1.0,
            candidate=0.5,
            relative_change=-0.5,
            tolerance=0.0,
            passed=False,
        )
        decided = gate([build(**{vetoing: [failing]})])[0]
        assert decided.separated is True and decided.holm_rejected is True, "it WON the comparison"
        assert decided.promoted is False, "and the check vetoed it"
        assert headline_line(render(decided)).startswith("BLOCKED BY A GUARDRAIL")
        assert "the check that vetoed" in headline_line(render(decided))

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_blocked_block_names_the_failing_check(self, gate, build) -> None:
        decided = gate([build(guardrails=[failing_cost_check()])])[0]
        assert any("cost (USD/row) FAILED" in note for note in decided.notes)

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_failed_check_note_stays_off_a_candidate_that_simply_lost(self, gate, build) -> None:
        """The note claims a veto and cites the BLOCKED headline — both false on a plain loss.

        `promoted` is already False for a candidate Holm never rejected, so a failing guardrail
        forced nothing, and the headline is NOT PROMOTED. Printing the note there is the same
        misdirection the BLOCKED rung's `holm_rejected` conjunct exists to remove: it sends the
        reader to fix cost when the real problem is power. The failing check is still visible in
        the rendered Guardrails list on this path — only the CLAIM is withheld.
        """
        decided = gate([build(p_value=0.9, guardrails=[failing_cost_check()])])[0]
        assert decided.holm_rejected is False
        assert decided.promoted is False
        assert not any("FAILED" in note for note in decided.notes), decided.notes
        # And the ordinary negative result is what the block DOES say.
        assert any("did not clear the Holm threshold" in note for note in decided.notes)

    @pytest.mark.parametrize(
        ("gate", "build", "refusing"),
        [
            # Each track refuses through its OWN mechanism, and they are not interchangeable.
            # `holm_promote` RECOMPUTES `gate_refusal` from the discreteness floor and overwrites
            # whatever the verdict arrived with, so setting the field directly is unreachable
            # there — a suite whose floor exceeds its Holm threshold is the reachable state.
            # `execution_gate` sets the field itself and `holm_promote_execution` only reads it.
            pytest.param(holm_promote, parity_activation, {"p_floor": 0.9, "n_discordant": 1}, id="activation"),
            pytest.param(
                holm_promote_execution, parity_execution, {"gate_refusal": "no comparison was made"}, id="execution"
            ),
        ],
    )
    def test_the_failed_check_note_stays_off_a_refused_verdict(self, gate, build, refusing) -> None:
        # Under a refusal the headline is not a decision at all, so a note asserting the block
        # "reports it as BLOCKED BY A GUARDRAIL" contradicts the line above it.
        decided = gate([build(guardrails=[failing_cost_check()], **refusing)])[0]
        assert decided.gate_refusal is not None, "the fixture must actually refuse"
        assert decided.promoted is False
        assert not any("FAILED" in note for note in decided.notes), decided.notes

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_an_empty_guardrail_list_does_not_block(self, gate, build) -> None:
        assert gate([build()])[0].promoted is True

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_holm_rejected_is_recorded_on_the_measured_branch(self, gate, build) -> None:
        # A p that clears alpha/1 and one that does not, in a family of one each.
        assert gate([build(p_value=0.001)])[0].holm_rejected is True
        assert gate([build(p_value=0.9)])[0].holm_rejected is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_holm_rejected_is_false_not_none_outside_the_family(self, gate, build) -> None:
        """`None` would read as "Holm has not run" on a verdict it has."""
        decided = gate([build(p_value=None, mean_diff=None, ci_low=None, ci_high=None)])[0]
        assert decided.holm_rejected is False
        assert decided.promoted is False

    # Deliberately NOT parametrized over `_TRACKS`: `separated` is a property of the VERDICT and
    # no gate is involved, so pairing it with a gate would run every case twice for nothing.
    @pytest.mark.parametrize("build", [parity_activation, parity_execution], ids=["activation", "execution"])
    @pytest.mark.parametrize(
        ("mean_diff", "ci_low", "expected"),
        [
            (0.5, 0.2, True),
            # The two boundaries. `> 0.0` on both, so a bound sitting exactly ON zero has not
            # excluded it and a zero difference does not favour the candidate.
            (0.5, 0.0, False),
            (0.0, 0.2, False),
            (-0.5, -0.2, False),
        ],
    )
    def test_separated_agrees_across_both_verdict_types(self, build, mean_diff, ci_low, expected) -> None:
        assert build(mean_diff=mean_diff, ci_low=ci_low).separated is expected


class TestGateResampleCount:
    """The gate decides on the p; a report renders it. The two counts are deliberately different."""

    def test_gate_defaults_to_the_gate_resample_count(self, tmp_path: Path) -> None:
        # The one test that exercises the SIGNATURE default — everything else passes a small count.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        verdict = activation_gate(
            incumbent_run_dirs=shared_dirs(tmp_path, incumbent, candidate),
            candidate_run_dirs=shared_dirs(tmp_path / "b", incumbent, candidate),
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
        )
        assert verdict.n_resamples == GATE_RESAMPLES
        assert GATE_RESAMPLES > BOOTSTRAP_RESAMPLES

    def test_gate_resamples_is_derived_from_its_inputs(self) -> None:
        # Recomputed, so the constant cannot be hand-edited away from its own derivation.

        assert math.ceil(2.0 / (GATE_P_PRECISION**2 * (DEFAULT_ALPHA / GATE_MAX_FAMILY))) == GATE_RESAMPLES

    def test_every_gate_default_uses_the_gate_resample_count(self) -> None:
        """The guardrail against a future gate function inheriting the report-grade default.

        Derived from the module rather than a list here: a new public callable that takes
        `n_resamples` is covered without anyone remembering to extend this.
        """

        import coder_eval.optimize.gate as gate

        offenders = []
        for name in dir(gate):
            if name.startswith("_"):
                continue
            obj = getattr(gate, name)
            if not callable(obj) or getattr(obj, "__module__", None) != gate.__name__:
                continue
            try:
                param = inspect.signature(obj).parameters.get("n_resamples")
            except (TypeError, ValueError):  # not introspectable
                continue
            # A REQUIRED `n_resamples` has no default to get wrong — the caller must supply the
            # count, which is the safe shape. Only a defaulted one can silently be the report-grade
            # figure. `note_resolution_degraded` is the one function this distinction is needed for:
            # it takes the count required and became visible to this scan when the package's
            # cross-module names lost their underscore.
            if (
                param is not None
                and param.default is not inspect.Parameter.empty
                and param.default is not GATE_RESAMPLES
            ):
                offenders.append(f"{name}(n_resamples={param.default!r})")
        assert not offenders, (
            f"{offenders} default to something other than GATE_RESAMPLES. Everything in this module "
            "feeds a promotion decision, and BOOTSTRAP_RESAMPLES is the report-grade count — at 2,000 "
            "draws a perfect 8-row candidate's p straddles its own Holm threshold across seeds."
        )

    def test_alpha_is_declared_once(self) -> None:

        assert inspect.signature(holm_promote).parameters["alpha"].default == DEFAULT_ALPHA
        assert inspect.signature(holm_rejections).parameters["alpha"].default == DEFAULT_ALPHA
        # `0\.05(?!\d)` rather than a substring test: a bare `"0.05" in line` also matches 0.056
        # and 0.0501, so a comment quoting an unrelated measured figure would fail this for a
        # reason that has nothing to do with alpha.
        alpha_literal = re.compile(r"0\.05(?!\d)")
        for module in (*_OPTIMIZE_MODULES, "reports_stats"):
            source = module_source(module)
            literals = [ln for ln in source.splitlines() if alpha_literal.search(ln) and "DEFAULT_ALPHA = " not in ln]
            assert not literals, f"{module} still spells 0.05 outside DEFAULT_ALPHA: {literals}"


class TestBothTracksEmitTheResolutionNote:
    """One function, two call sites, and the same sentence — the reason these notes are shared.

    `note_holm_family` and `note_ordinary_negative` are shared for exactly this: byte-identical
    copies 600 lines apart drift, and a ledger read back weeks later then has the two tracks
    describing one decision differently.
    """

    _FAMILY = GATE_MAX_FAMILY + 3

    def _activation_family(self, tmp_path: Path) -> list[ActivationGateVerdict]:
        incumbent, candidate = tiny_suite(6, 6)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        return [activation_verdict(run_dirs) for _ in range(self._FAMILY)]

    def _execution_family(self, tmp_path: Path) -> list[ExecutionGateVerdict]:
        return [exec_gate(exec_run_dir(tmp_path / f"g{i}", **WINNER)) for i in range(self._FAMILY)]

    @staticmethod
    def _note(verdict) -> str:
        return next(n for n in verdict.notes if n.startswith("resolution:"))

    def test_the_two_tracks_emit_the_identical_sentence(self, tmp_path: Path) -> None:
        activation = holm_promote(self._activation_family(tmp_path / "a"))[0]
        execution = holm_promote_execution(self._execution_family(tmp_path / "e"))[0]
        # Same family size, same draw count -> the same string, character for character.
        assert self._note(activation) == self._note(execution)
        assert f"family of {self._FAMILY}" in self._note(activation)

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_a_family_at_the_sized_limit_emits_nothing_on_either_track(self, tmp_path: Path, track: str) -> None:
        if track == "activation":
            incumbent, candidate = tiny_suite(6, 6)
            run_dirs = shared_dirs(tmp_path, incumbent, candidate)
            decided = holm_promote([activation_verdict(run_dirs) for _ in range(GATE_MAX_FAMILY)])
        else:
            verdicts = [exec_gate(exec_run_dir(tmp_path / f"g{i}", **WINNER)) for i in range(GATE_MAX_FAMILY)]
            decided = holm_promote_execution(verdicts)
        assert not any(note.startswith("resolution:") for v in decided for note in v.notes)

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_the_family_names_its_smallest_draw_count(self, tmp_path: Path, track: str) -> None:
        """A mixed family is bounded by its coarsest member, not by its first or its finest.

        Each member's own `n_resamples` is on its verdict, so a naive implementation reads whichever
        verdict it happens to be decorating and reports a different resolution per member — for one
        family, decided at one threshold.
        """
        # BELOW the fixtures' own default (`FAST_RESAMPLES`), which is what makes this member the
        # coarsest — a "coarse" count above it would be the family minimum for the wrong reason and
        # the test would pass against a `max`.
        coarse = FAST_RESAMPLES // 2
        if track == "activation":
            incumbent, candidate = tiny_suite(6, 6)
            run_dirs = shared_dirs(tmp_path, incumbent, candidate)
            verdicts = [
                activation_verdict(run_dirs, n_resamples=coarse),
                *[activation_verdict(run_dirs) for _ in range(self._FAMILY - 1)],
            ]
            decided = holm_promote(verdicts)
        else:
            first = exec_gate(exec_run_dir(tmp_path / "g0", **WINNER), n_resamples=coarse)
            rest = [exec_gate(exec_run_dir(tmp_path / f"g{i}", **WINNER)) for i in range(1, self._FAMILY)]
            decided = holm_promote_execution([first, *rest])
        expected = expected_resolution_note(self._FAMILY, coarse)
        # EVERY member reports the family's resolution, including the ones drawn more finely.
        for verdict in decided:
            assert f"{expected[1]:.4f}" in self._note(verdict)
            assert f"at {coarse} draws" in self._note(verdict)

    def test_it_survives_a_refusal_because_it_is_not_a_claim_about_the_candidate(self, tmp_path: Path) -> None:
        # Beside the family note and the resolution-floor note, outside the negative-result guard: a
        # statement about the draw count stays true whatever the refusal says.
        incumbent, candidate = tiny_suite(3, 3)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdicts = [activation_verdict(run_dirs, n_resamples=REFUSAL_RESAMPLES) for _ in range(self._FAMILY)]
        decided = holm_promote(verdicts)[0]
        assert decided.gate_refusal is not None, "fixture drifted — this is the discreteness refusal"
        assert self._note(decided).startswith("resolution:")

    def test_the_decision_itself_is_untouched(self, tmp_path: Path) -> None:
        # A note, never a refusal: `gate_refusal`, `promoted` and `separated` are what a caller acts
        # on, and none of them may move because the family grew past the sized limit.
        verdicts = self._execution_family(tmp_path)
        decided = holm_promote_execution(verdicts)
        for verdict in decided:
            assert verdict.gate_refusal is None
            assert verdict.separated is True
            assert verdict.promoted == verdict.holm_rejected


_NEGATIVE_RESULT_FRAGMENTS = (
    "not promoted:",
    "did not clear the Holm threshold",
    "still contains zero",
    "the interval separates",
    "FAILED — this forces",
)


def _negative_result_notes(notes: list[str]) -> list[str]:
    """Every note that reads as a claim about a CANDIDATE that lost.

    Read from the note LIST, never by `not in` over the rendered page: the block legitimately
    contains the words "not promoted" in other sentences, and a `.replace()`d-string absence
    assertion is the vacuity trap this repo has already been bitten by.
    """
    return [note for note in notes if any(fragment in note for fragment in _NEGATIVE_RESULT_FRAGMENTS)]


class TestARefusalSuppressesNegativeResultProse:
    """Under a refusal, no note may claim the candidate lost — on EITHER track.

    A refusal says the comparison could not decide anything, so a negative-result note beneath it
    is a second, contradictory claim in one block — on the page a user pastes into a promotion
    ledger. `holm_promote_execution` has always guarded its whole ladder with one `if not refused:`;
    `holm_promote` guarded exactly one of its four rungs, so three fired regardless.

    The cross-product below is the point. Written as hand-picked cases, the three unguarded rungs
    were individually plausible and nobody noticed the combination; asserting ONE RULE over every
    combination is what makes the ladder a contract rather than three independently-remembered
    guards.

    **The MDE axis is deliberately absent from it, and that is a layering fact rather than a gap.**
    The full invariant is "under a refusal, no negative-result note AND no MDE advisory". Neither
    `holm_promote` nor `holm_promote_execution` can emit an MDE advisory — those are written in
    `activation_gate` and `_execution_diagnostics`, before either Holm wrapper runs — so varying
    `mde` here would add inert cases. The advisory half is asserted where it is producible:
    `TestExecutionGateRefusesAReusedRunDir::test_refused_already_is_reachable_as_true` and
    `TestExecutionDiagnostics::test_refused_already_suppresses_both_advisory_notes`, which cover a
    branch each.

    **On the activation track the exclusion is also a real asymmetry, not only layering.** A
    discreteness refusal is set inside `holm_promote`, AFTER `activation_gate` has already written
    its MDE notes, and nothing suppresses them — unlike the execution track, where
    `refused_already` does. Both are resolution statements, so they reinforce the refusal rather
    than contradict it and `promoted` is unaffected; it is stated here rather than presented as
    pure layering.
    """

    @pytest.mark.parametrize("mean_diff", [0.5, -0.3, 0.0])
    @pytest.mark.parametrize(("ci_low", "ci_high"), [(0.2, 0.75), (-0.5, -0.1), (-0.05, 0.6)])
    @pytest.mark.parametrize("p_value", [0.001, 0.9])
    @pytest.mark.parametrize("failing_sibling", [False, True])
    def test_the_activation_ladder_is_silent_under_a_refusal(
        self, mean_diff, ci_low, ci_high, p_value, failing_sibling
    ) -> None:
        sibling = [
            GuardrailCheck(
                name="sibling recall.yes [criterion 1]",
                incumbent=1.0,
                candidate=0.5,
                relative_change=-0.5,
                tolerance=0.0,
                passed=False,
            )
        ]
        # `p_floor` above every Holm threshold in a family of one, so `_refusal_message` fires
        # whatever the other axes do.
        refused = parity_activation(
            mean_diff=mean_diff,
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
            p_floor=0.9,
            n_discordant=1,
            sibling_checks=sibling if failing_sibling else [],
            guardrails=[failing_cost_check()],
        )
        decided = holm_promote([refused])[0]
        assert decided.gate_refusal is not None, "the fixture must refuse for this to mean anything"
        assert decided.promoted is False
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert headline_line(render_markdown(decided)).startswith("CANNOT SEPARATE AT THIS SIZE")

    @pytest.mark.parametrize("mean_diff", [0.5, -0.3])
    @pytest.mark.parametrize(("ci_low", "ci_high"), [(0.2, 0.75), (-0.5, -0.1), (-0.05, 0.6)])
    @pytest.mark.parametrize("p_value", [0.001, 0.9])
    def test_the_execution_ladder_is_silent_under_a_refusal(self, mean_diff, ci_low, ci_high, p_value) -> None:
        refused = parity_execution(
            mean_diff=mean_diff,
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
            gate_refusal="the observed difference is below this suite's MDE",
            guardrails=[failing_cost_check()],
        )
        decided = holm_promote_execution([refused])[0]
        assert decided.promoted is False
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT")

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            # Three of these four sentences are SHARED `_note_*` declarations, so the expected
            # fragment is the same on both tracks. The wrong-direction rung is deliberately not
            # shared — activation says "the interval separates in the incumbent's favour", the
            # execution track says "the paired difference favours the incumbent", because each
            # names its own statistic — so only their common prefix is asserted here.
            ({"p_value": 0.9}, "did not clear the Holm threshold"),
            ({"mean_diff": -0.3, "ci_low": -0.5, "ci_high": -0.1}, "not promoted:"),
            ({"ci_low": -0.05}, "still contains zero"),
            ({"guardrails": [failing_cost_check()]}, "FAILED — this forces"),
        ],
        ids=["lost", "wrong-direction", "ci-contains-zero", "vetoed"],
    )
    def test_the_same_verdicts_do_produce_those_notes_without_a_refusal(self, gate, build, kwargs, expected) -> None:
        """Anti-vacuity for the suppression tests above, and for the fragment list itself.

        If `_NEGATIVE_RESULT_FRAGMENTS` stopped matching anything the suppression assertions would
        pass over an empty list forever — indistinguishable from a working guard. Every case here
        is witnessed against a verdict that genuinely earns a negative-result note. Three of the
        four pin the exact shared sentence; the wrong-direction case pins only the `"not promoted:"`
        prefix the two tracks share, because each names its own statistic after it.
        """
        decided = gate([build(**kwargs)])[0]
        assert decided.gate_refusal is None, "this half must NOT refuse"
        notes = _negative_result_notes(decided.notes)
        assert notes, decided.notes
        assert any(expected in note for note in notes), notes

    def test_the_reproduced_contradiction_is_gone(self) -> None:
        """The exact verdict the reviewer reproduced against shipped code.

        It rendered `**CANNOT SEPARATE AT THIS SIZE — … so this is NOT a negative result about
        it.**` with `not promoted: the interval separates in the incumbent's favour.` directly
        beneath. `promoted` was already correctly False — the defect was confined to the prose.
        """
        verdict = parity_activation(
            p_value=0.01, p_floor=0.2, mean_diff=-0.3, ci_low=-0.5, ci_high=-0.1, rows_paired=6, n_discordant=1
        )
        decided = holm_promote([verdict])[0]
        assert decided.gate_refusal is not None
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert decided.promoted is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_family_and_near_floor_notes_survive_a_refusal(self, gate, build) -> None:
        """They are NOT negative-result rungs, and must not be swept into the guard.

        "Holm applied across a family of N" stays true under a refusal, and the execution track
        keeps its family note outside `if not refused:` for exactly that reason. Sweeping them in
        would leave a refused block unable to say what family it was decided in.
        """
        refusing = {"p_floor": 0.9, "n_discordant": 1} if build is parity_activation else {"gate_refusal": "refused"}
        decided = gate([build(**refusing)])[0]
        assert decided.gate_refusal is not None
        assert any("Holm applied across a family of" in note for note in decided.notes)
        if build is parity_activation:
            # The near-floor note is the other rung that must NOT be swept in: it is a statement
            # about the DRAW COUNT's resolution, which stays true whatever the refusal says. The
            # fixture's p = 0.001 against a 2,000-draw floor of 0.0010 puts it inside
            # NEAR_FLOOR_MULTIPLE, so this is a real witness rather than a vacuous branch.
            assert any("at or near this bootstrap's resolution floor" in note for note in decided.notes)


class TestTheTwoDeduplications:
    """`wrong_path_reason` and `holm_family` each replace a byte-identical copy.

    A dedup that changes user-facing text is a silent report change, so the messages are asserted
    against pre-change LITERALS rather than against each other — comparing the two call sites to
    one another would pass just as happily if both had moved together.
    """

    def test_the_wrong_path_message_is_byte_identical_to_before(self, tmp_path: Path) -> None:
        expected = (
            f"nothing matched <run>/incumbent/{SUITE}/*/*/task.json under {tmp_path}/a, {tmp_path}/b — "
            "that is a wrong variant id, a wrong suite id or a wrong run directory, not a measurement"
        )
        assert wrong_path_reason("incumbent", SUITE, [tmp_path / "a", tmp_path / "b"]) == expected

    def test_both_floors_emit_that_message_verbatim(self, tmp_path: Path, caplog) -> None:
        dirs = [tmp_path / "a", tmp_path / "b"]
        expected = wrong_path_reason("incumbent", SUITE, dirs)
        for call in (
            lambda: measure_noise_floor(
                run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0, model="m"
            ),
            lambda: execution_floor(dirs),
        ):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                assert call() is None
            assert expected in caplog.text

    def test_the_empty_run_dirs_divergence_is_folded_in(self) -> None:
        """The ONE real difference the two copies had, and the reason to collapse them.

        The execution twin appended `or "no run dirs were given"` and the activation one did not,
        so an empty sequence read as a message trailing `under ` on one track and named the case on
        the other. Both now name it — the fuller form wins, which loses nothing.
        """
        assert "under no run dirs were given" in wrong_path_reason("incumbent", SUITE, [])

    def test_holm_family_maps_original_indices_with_an_excluded_verdict_in_the_middle(self) -> None:
        """The off-by-one a naive `enumerate` over the filtered list produces.

        The `None`-p verdict is deliberately in the MIDDLE: with it last, filtered and original
        indices agree and a broken mapping still passes.
        """
        verdicts = [
            parity_activation(p_value=0.001),
            parity_activation(p_value=None, mean_diff=None, ci_low=None, ci_high=None),
            parity_activation(p_value=0.002),
        ]
        family, rejected_at = holm_family(verdicts, DEFAULT_ALPHA)
        assert [i for i, _p in family] == [0, 2], "membership is by ORIGINAL index"
        assert rejected_at == {0, 2}
        # And end to end: the excluded verdict is index 1, and its neighbours keep their decisions.
        decided = holm_promote(verdicts)
        assert [v.holm_rejected for v in decided] == [True, False, True]
        assert [v.promoted for v in decided] == [True, False, True]

    def test_holm_family_handles_an_empty_family(self) -> None:
        verdicts = [parity_activation(p_value=None, mean_diff=None, ci_low=None, ci_high=None)]
        family, rejected_at = holm_family(verdicts, DEFAULT_ALPHA)
        assert family == [] and rejected_at == set()

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_promoted_vector_is_unchanged_by_the_extraction(self, gate, build) -> None:
        """Both wrappers decide the same family the same way after routing through one helper.

        Holm's step-down over `[0.001, 0.02, 0.9]` at alpha 0.05: 0.001 <= 0.05/3 rejects,
        0.02 <= 0.05/2 rejects, 0.9 > 0.05/1 stops. The thresholds are spelled out because the
        answer is not the one a reader guesses — an uncorrected `p <= alpha` gives the same first
        two and would pass a test that only asserted the last.
        """
        family = [build(p_value=p) for p in (0.001, 0.02, 0.9)]
        decided = gate(family)
        assert [v.holm_rejected for v in decided] == [True, True, False]
        assert [v.promoted for v in decided] == [True, True, False]
        # And the correction genuinely bites: the same p in a family of FOUR is decided against
        # alpha/4 = 0.0125 and no longer rejects.
        assert gate([build(p_value=0.02) for _ in range(4)])[0].holm_rejected is False


class TestStageAReadersReconcileTheTree:
    """CE053 in behaviour: every run-tree reader either refuses or warns on a contaminated tree.

    Both gates already refused a run dir holding results its own `run.json` never wrote. The four
    Stage A / floor readers did not — measured, on dirs `activation_gate` correctly refuses,
    `measure_noise_floor` returned a floor computed over an extra pooled row and `arm_row_scores`
    returned the stale row in its vector. The floor decides whether a round runs at all; the
    vectors feed all three Pareto fronts.

    The RESPONSE differs by return type and that asymmetry is the phase's whole decision: a
    `NoiseFloor | None` can refuse, an `ArmRowScores` has nowhere to put a refusal.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {
        "r1": [("yes", "yes")],
        "r2": [("yes", "no")],
        "r3": [("no", "no")],
    }

    def _contaminate(self, run_dirs: list[Path], variant: str = "incumbent") -> None:
        """One row nothing recorded, in the LAST dir — an earlier invocation of a re-used --run-dir."""
        write_row(run_dirs[-1], variant, "stale", eval_result("stale", [("yes", "yes")]), record=False)

    def test_the_activation_floor_refuses_and_names_the_directory_and_a_pair(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=FAST_RESAMPLES,
            )
        assert floor is None
        assert f"{run_dirs[-1]}/incumbent" in caplog.text
        assert "stale/00" in caplog.text, "the (row, replicate) pair is what a reader acts on"

    def test_the_execution_floor_refuses_the_same_way(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {f"r{i}": [0.1 * i, 0.2 * i, 0.3] for i in range(4)})
        write_row(run_dirs[0], "incumbent", "stale", scored_result("stale", 1.0), 7, record=False)
        with caplog.at_level(logging.WARNING):
            assert execution_floor(run_dirs) is None
        assert f"{run_dirs[0]}/incumbent" in caplog.text
        assert "stale/07" in caplog.text

    def test_arm_row_scores_warns_and_still_returns_a_vector(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The warn-not-refuse half: `ArmRowScores` has no field a refusal could live in, so the
        # answer is still produced — with the same message the floors refuse with.
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert [a.variant_id for a in arms] == ["incumbent"]
        assert set(arms[0].row_scores) == {"r1", "r2", "r3", "stale"}
        assert "results that no recorded invocation wrote" in caplog.text
        assert f"{run_dirs[-1]}/incumbent" in caplog.text

    def test_cost_quality_points_warns_exactly_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """The regression guard for the double-reconcile the plan rejected (S8).

        `cost_quality_points` reaches the tree through `arm_row_scores`, which reconciles. A
        second reconcile here would read every run.json twice per arm and warn twice about one
        fault — so the suppression is the record, and this test is what keeps it true.
        """
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert caplog.text.count("results that no recorded invocation wrote") == 1

    def test_one_warning_per_sweep_however_many_arms_share_a_run_dir(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Both arms live under one run dir, as a real experiment writes them. Reconciling inside
        # the per-arm loop would read that dir's run.json once per arm.
        run_dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        self._contaminate(run_dirs, "candidate")
        with caplog.at_level(logging.WARNING):
            arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent", "candidate"], suite_id=SUITE, criterion_index=0)
        assert caplog.text.count("Row scores may be over a contaminated tree") == 1

    def test_a_clean_tree_is_byte_identical_and_silent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=FAST_RESAMPLES,
            )
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert floor is not None and floor.n_rows == 3
        assert set(arms[0].row_scores) == {"r1", "r2", "r3"}
        assert caplog.text == ""

    def test_an_unknown_run_dir_does_not_refuse(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A `run.json` that records no `task_results` is a NOTE, never a refusal.

        The module's settled missing-provenance stance: old run dirs stay measurable, and the one
        state where contamination is undetectable must not also be the one that refuses everything.
        Asserted at WARNING level, because a floor that WAS measured must not log as if it was not.
        """
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        for run_dir in run_dirs:
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload.pop("task_results", None)
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=FAST_RESAMPLES,
            )
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert floor is not None
        assert set(arms[0].row_scores) == {"r1", "r2", "r3"}
        assert caplog.text == ""

    def test_a_wrong_path_still_blames_the_path_not_the_tree(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The reconcile runs BEFORE the load, so the ordering has to be checked: a wrong variant
        # leaves nothing on disk to be unrecorded, and the wrong-path message must still win.
        run_dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(
                    run_dirs=run_dirs,
                    variant_id="typo",
                    suite_id=SUITE,
                    criterion_index=0,
                    model="claude-haiku-4-5",
                )
                is None
            )
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "no recorded invocation wrote" not in caplog.text


class TestTheClassifierIsShared:
    """One declaration, at rank 1, and a copy in either track module must fail the build."""

    def test_it_lives_in_the_shared_gate_module(self) -> None:
        import coder_eval.optimize.gate as gate

        assert classify_confirm.__module__ == gate.__name__
        assert build_confirm_verdict.__module__ == gate.__name__
        assert decide_family.__module__ == gate.__name__

    @pytest.mark.parametrize("module", ["optimize.activation", "optimize.execution"])
    def test_neither_track_module_defines_its_own(self, module: str) -> None:
        # A SOURCE scan, not an attribute check: both modules IMPORT these names, so `hasattr` passes
        # either way and only the source says whether a second implementation was written.
        source = module_source(module)
        for name in ("classify_confirm", "build_confirm_verdict", "decide_family"):
            assert f"def {name}(" not in source, (
                f"{module} defines its own {name} — the two track modules may not import each "
                "other, so a per-track copy is two copies of promotion-relevant arithmetic"
            )

    def test_both_tracks_call_the_shared_builder(self) -> None:
        for module in ("optimize.activation", "optimize.execution"):
            assert "build_confirm_verdict(" in module_source(module)


_VERDICT_PINS = Path(__file__).parent / "_fixtures" / "optimize_verdicts"


def _assert_matches_pin(verdict, name: str) -> None:
    """Compare a verdict's dump against output captured BEFORE the construction rewrite.

    That rewrite replaced `verdict_kwargs` / `_verdict(**overrides)` with literal keywords on
    every construction path of both models. It is behaviour-preserving BY CONSTRUCTION — which is
    exactly the kind of claim that needs a witness, because a dropped or mis-spelled keyword shows
    up as a field quietly at its default rather than as an error.

    Compared as PARSED JSON so the pinned floats are the ones the run actually produced, and so a
    field ADDED to either model fails here rather than being silently absent from the expectation.
    Committed beside the other characterization fixtures (`report_snapshots`, `golden_streams`).

    They have since outlived that one job and become ordinary characterization snapshots: an
    INTENTIONAL change to what a verdict says will fail here too, and the fixture is then updated
    with the diff reviewed. That is the point — every field of a promotion verdict is something a
    reader acts on, so none of them should be able to move without someone looking at it.
    """
    expected = json.loads((_VERDICT_PINS / f"{name}.json").read_text(encoding="utf-8"))
    assert json.loads(verdict.model_dump_json()) == expected


class TestConstructionIsBehaviourPreserving:
    """The three construction paths the rewrite touches, pinned against pre-rewrite output."""

    def test_the_activation_verdict_is_unchanged(self, tmp_path: Path) -> None:
        _assert_matches_pin(activation_verdict(shared_dirs(tmp_path, *pinned_suite())), "activation_gate")

    def test_the_activation_early_return_is_unchanged(self, tmp_path: Path) -> None:
        # The `bootstrap is None` path, which used to OVERWRITE two keys in the shared dict before
        # splatting it — the path the rewrite changes most. The incumbent scores three rows across
        # three invocations while only one PAIRS, so the bootstrap declines (1 scored row) while
        # the incumbent's own null split still yields a real `mde`: an early-return pin whose
        # `mde` was `None` could not see a dropped `mde=` keyword.
        incumbent = {f"p{i}": [("yes", "no"), ("yes", "yes")] for i in range(3)}
        candidate = {"p0": [("yes", "yes"), ("yes", "no")]}
        _assert_matches_pin(
            activation_verdict(shared_dirs(tmp_path, incumbent, candidate)), "activation_gate_early_return"
        )

    def test_the_execution_verdict_is_unchanged(self, tmp_path: Path) -> None:
        _assert_matches_pin(exec_gate(exec_run_dir(tmp_path, **WINNER)), "execution_gate")

    def test_the_refused_execution_verdict_is_unchanged(self, tmp_path: Path) -> None:
        # `gate_refusal` is the one value `_verdict` reads from its closure rather than taking as a
        # parameter, and it is `None` on every healthy verdict — so a pin that never refuses cannot
        # see it dropped.
        _assert_matches_pin(exec_gate(exec_run_dir(tmp_path, **uniform_shift(4))), "execution_gate_refused")


class TestTheHolmLoopIsOneLoop:
    """`decide_family` is the single promotion loop, and these are the properties it now owns alone.

    The loop was written twice, 700 lines apart. Every assertion here used to be true of two
    implementations that happened to agree; it is now true of one, and the parametrization over both
    tracks is what proves the collapse did not quietly change either track's answer.
    """

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_two_trailing_notes_appear_exactly_once(self, gate, build) -> None:
        """The double-append trap.

        `_activation_notes` appended `note_holm_family` / `note_resolution_degraded` itself while
        the execution wrapper did it inline. `decide_family` owns both for both tracks now, so a
        ladder that kept its own append prints the sentence twice — and every reader of the rendered
        block sees the family described to it twice, which reads as two families.
        """
        decided = gate([build()])[0]
        family_note = note_holm_family(1, DEFAULT_ALPHA)
        assert decided.notes.count(family_note) == 1, decided.notes
        # And nothing else in the block starts the same way, which is what a duplicate would.
        assert sum(note.startswith("Holm applied across a family") for note in decided.notes) == 1

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_the_resolution_note_appears_exactly_once_too(self, tmp_path: Path, track: str) -> None:
        # The conditional trailing note, on a family past the size the gate is sized for — the only
        # state in which it is emitted at all, so a duplicate is invisible below it.
        family = GATE_MAX_FAMILY + 3
        if track == "activation":
            incumbent, candidate = tiny_suite(6, 6)
            run_dirs = shared_dirs(tmp_path, incumbent, candidate)
            decided = holm_promote([activation_verdict(run_dirs) for _ in range(family)])
        else:
            verdicts = [exec_gate(exec_run_dir(tmp_path / f"g{i}", **WINNER)) for i in range(family)]
            decided = holm_promote_execution(verdicts)
        for verdict in decided:
            assert sum(note.startswith("resolution:") for note in verdict.notes) == 1, verdict.notes

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_threshold_is_per_rank_and_not_recomputed_per_verdict(self, gate, build) -> None:
        """Two candidates at the same p must get the same answer, and it must be Holm's.

        A family of two at p = 0.03 with alpha = 0.05: the strictest rank's threshold is
        alpha/2 = 0.025, which 0.03 does not clear, so Holm's step-down stops and NEITHER is
        rejected. A loop that compared each p against a bare `alpha` would reject both — the
        uncorrected degeneration this correction exists to prevent, and the two answers differ.
        """
        decided = gate([build(p_value=0.03), build(p_value=0.03)])
        assert [v.holm_rejected for v in decided] == [False, False]
        assert [v.promoted for v in decided] == [False, False]
        assert all(v.holm_alpha == DEFAULT_ALPHA for v in decided)

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_a_refused_verdict_with_a_real_p_stays_in_the_family(self, gate, build) -> None:
        """Membership is `p_value is not None` and nothing else, on both tracks.

        Dropping a refused-but-measured verdict would shrink `m` and LOOSEN `alpha/m` for its
        siblings — the uncorrected-`p <= alpha` degeneration approached from the other side. The
        family SIZE in the shared note is the observable, so this asserts on it rather than on an
        internal.
        """
        healthy = build(p_value=0.001)
        refused = (
            build(p_value=0.001, p_floor=0.9, n_discordant=1)
            if build is parity_activation
            else build(p_value=0.001, gate_refusal="the two arms differed by an identical amount on every row")
        )
        decided = gate([healthy, refused])
        assert decided[1].gate_refusal is not None, "the fixture must actually refuse"
        # Both members see a family of TWO — the refused one was tested, so it counts.
        for verdict in decided:
            assert note_holm_family(2, DEFAULT_ALPHA) in verdict.notes

    def test_the_execution_refusal_is_read_back_byte_for_byte(self) -> None:
        """`decide_family` writes `gate_refusal` on every measured path, so a no-op write must be one.

        The unified loop writes the hook's returned refusal where the execution wrapper used to omit
        the key entirely. The hook returns `verdict.gate_refusal` unchanged, so the write is a
        no-op — and "is a no-op" is a claim about a field a user reads to decide whether a block is
        a decision at all, which makes it a claim that needs a witness.
        """
        message = (
            "there is no experiment file at /nowhere/experiment.json, so the paired statistic could not be computed"
        )
        decided = holm_promote_execution([parity_execution(p_value=0.001, gate_refusal=message)])[0]
        assert decided.gate_refusal == message
        assert decided.promoted is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_an_unmeasured_verdict_is_outside_the_family_and_says_so(self, gate, build) -> None:
        decided = gate([build(p_value=None, mean_diff=None, ci_low=None, ci_high=None)])[0]
        assert decided.promoted is False
        assert decided.holm_rejected is False, "None would read as 'Holm has not run' on a verdict it has"
        assert decided.holm_alpha == DEFAULT_ALPHA
        assert NOTE_OUTSIDE_FAMILY in decided.notes

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_an_unmeasured_refused_verdict_withholds_the_outside_family_note(self, gate, build) -> None:
        """The `gate_refusal is None` guard on the unmeasured branch, asserted on both tracks.

        Reachable on each: the activation track's cross-split preflight and every execution-track
        "there was no comparison to make" cause set the field with no p. Unguarded, the block prints
        an ordinary negative-result note directly under a refusal headline.
        """
        decided = gate(
            [build(p_value=None, mean_diff=None, ci_low=None, ci_high=None, gate_refusal="no comparison was made")]
        )[0]
        assert decided.gate_refusal == "no comparison was made"
        assert NOTE_OUTSIDE_FAMILY not in decided.notes
        assert decided.promoted is False
        assert decided.holm_rejected is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_family_size_is_the_measured_count(self, gate, build) -> None:
        """`len(family)`, never `len(verdicts)` — and the two differ exactly when it matters.

        Every other case in this class uses a family whose members are all measured, where the two
        counts coincide; a mutation to `len(verdicts)` passes the entire suite. So this is the one
        state that separates them: one member with no p, which the membership rule
        (`p_value is not None`) excludes.

        The observable is that BOTH sentences naming the family derive from the same number. The
        ladder's rung and the trailing note compute it independently, so under the mutation one
        block reports two different family sizes — and a reader checking "is alpha/m the bar it
        says it is" cannot tell which one to believe.
        """
        unmeasured = build(p_value=None, mean_diff=None, ci_low=None, ci_high=None)
        measured = build(p_value=0.5)
        decided = gate([unmeasured, measured])[1]
        assert note_holm_family(1, DEFAULT_ALPHA) in decided.notes
        # The ladder's own sentence, computed from `facts.family_size` rather than from the note.
        assert any("in a family of 1 " in note for note in decided.notes), decided.notes
        assert not any("family of 2" in note for note in decided.notes), decided.notes

    def test_the_activation_refusal_names_the_measured_family_size_too(self) -> None:
        """The second reader of `facts.family_size`: the discreteness refusal's own sentence.

        It is a separate code path from the ladder — computed in `_refusal_message` from the same
        fact — so a mutation that survived the ladder assertion above would still be reporting a
        bar the family does not have.
        """
        unmeasured = parity_activation(p_value=None, mean_diff=None, ci_low=None, ci_high=None)
        refused = parity_activation(p_value=0.001, p_floor=0.9, n_discordant=1)
        decided = holm_promote([unmeasured, refused])[1]
        assert decided.gate_refusal is not None
        assert "in a family of 1 " in decided.gate_refusal, decided.gate_refusal

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_note_order_is_the_rendered_order(self, gate, build) -> None:
        """Carried notes first, then the ladder, then the two trailing notes — in that order.

        The rendered pins are the real detector, but they only cover the states the fixtures reach.
        This states the rule: whatever a track's ladder says, the family note is LAST among the
        notes the loop adds, because it is a statement about the family rather than the candidate.
        """
        carried = "a note the gate already wrote"
        decided = gate([build(p_value=0.9, notes=[carried])])[0]
        assert decided.notes[0] == carried
        assert decided.notes[-1] == note_holm_family(1, DEFAULT_ALPHA)
        # And the ladder's ordinary-negative rung sits between them.
        assert any("did not clear the Holm threshold" in note for note in decided.notes[1:-1])


class TestTheSharedFieldsAreDeclaredOnce:
    """The 18 fields both tracks carry live on :class:`GateVerdictBase` and nowhere else.

    They used to be declared twice, in hand-maintained parity, and the parity was already broken:
    14 of the 18 carried different `description` text and four differed in required-ness. A
    re-declaration is now a licensed exception recorded in `_FIELD_OVERRIDES`, and this class
    asserts BOTH directions of that licence — an unrecorded override fails, and a recorded one that
    no longer differs from the base fails too. That second half is the CE038 `EXEMPT` pattern: a
    stale licence must not outlive the trade it recorded.
    """

    # The four statistic fields differ in DEFAULT, so they are the entries the whole Shape-B design
    # turns on; the other two differ in description alone and are the judgement calls.
    _STATISTIC_FIELDS = ("mean_diff", "ci_low", "ci_high", "p_value")

    @staticmethod
    def _declared_here(model: type[GateVerdictBase]) -> set[str]:
        """The field names the class body itself declares, inherited ones excluded.

        `model_fields` cannot answer this — it merges the base's fields in, so every base field
        reads as "declared" on both subclasses. `__annotations__` on the class object is
        class-local, which is exactly the question.
        """
        return set(getattr(model, "__annotations__", {}))

    def test_the_base_holds_the_eighteen_shared_fields(self) -> None:
        shared = set(ActivationGateVerdict.model_fields) & set(ExecutionGateVerdict.model_fields)
        assert set(GateVerdictBase.model_fields) == shared
        assert len(GateVerdictBase.model_fields) == 18

    def test_the_activation_verdict_overrides_nothing(self) -> None:
        overridden = self._declared_here(ActivationGateVerdict) & set(GateVerdictBase.model_fields)
        assert overridden == set(), f"{sorted(overridden)} is declared twice for no recorded reason"

    def test_every_override_is_recorded(self) -> None:
        licensed = {name for name, _reason in _FIELD_OVERRIDES}
        for model in (ActivationGateVerdict, ExecutionGateVerdict):
            overridden = self._declared_here(model) & set(GateVerdictBase.model_fields)
            unrecorded = overridden - licensed
            assert not unrecorded, (
                f"{model.__name__} re-declares {sorted(unrecorded)} without an entry in "
                "_FIELD_OVERRIDES — a base field spelled twice is the parity this base removed"
            )

    def test_the_recorded_overrides_are_exactly_the_known_set(self) -> None:
        # Pinned as a LIST, so a seventh licence — or a reordering that would move a base field —
        # is a visible decision rather than a quiet widening. Six is the agreed ceiling.
        assert [name for name, _reason in _FIELD_OVERRIDES] == [
            *self._STATISTIC_FIELDS,
            "n_resamples",
            "gate_refusal",
        ]

    @pytest.mark.parametrize(("name", "reason"), _FIELD_OVERRIDES, ids=[n for n, _r in _FIELD_OVERRIDES])
    def test_a_recorded_override_really_differs(self, name: str, reason: str) -> None:
        assert reason.strip(), "an entry records WHY, so a reader can judge whether it still applies"
        base = GateVerdictBase.model_fields[name]
        # The subclass that declares it — asserted rather than assumed, so a licence for a field
        # nobody overrides any more fails here instead of sitting unread.
        declaring = [
            model for model in (ActivationGateVerdict, ExecutionGateVerdict) if name in self._declared_here(model)
        ]
        assert declaring, f"_FIELD_OVERRIDES licenses {name}, which no subclass re-declares"
        for model in declaring:
            field = model.model_fields[name]
            differs = field.is_required() != base.is_required() or field.description != base.description
            assert differs, (
                f"{model.__name__}.{name} re-declares the base field identically — delete the "
                "override and the entry, or the licence outlives the trade"
            )

    @pytest.mark.parametrize(("name", "_reason"), _FIELD_OVERRIDES, ids=[n for n, _r in _FIELD_OVERRIDES])
    def test_an_override_keeps_its_base_position(self, name: str, _reason: str) -> None:
        """Pinned because `model_dump()` order follows it, and a pydantic release could change it.

        Over every licensed override rather than the four statistic ones: the property is about
        re-declaration itself, and a description-only override moves a key just as far.
        """
        expected = list(GateVerdictBase.model_fields).index(name)
        for model in (ActivationGateVerdict, ExecutionGateVerdict):
            if name in self._declared_here(model):
                assert list(model.model_fields).index(name) == expected

    @pytest.mark.parametrize("name", _STATISTIC_FIELDS)
    def test_the_statistic_fields_keep_their_per_track_required_ness(self, name: str) -> None:
        """The property the whole Shape-B design turns on, so it gets a test rather than a note."""
        activation = full_activation_verdict().model_dump()
        del activation[name]
        with pytest.raises(ValidationError) as raised:
            ActivationGateVerdict(**activation)
        assert [error["loc"] for error in raised.value.errors()] == [(name,)]

        execution = full_execution_verdict().model_dump()
        del execution[name]
        assert getattr(ExecutionGateVerdict(**execution), name) is None

    def test_an_override_restates_its_description(self) -> None:
        """A re-declaration replaces the whole `FieldInfo`, so a dropped `description=` is silent.

        Asked of the DECLARING class rather than of `ExecutionGateVerdict` by name: a licence taken
        up on the other track would resolve through inheritance to the base's own non-empty
        description and pass while saying nothing.
        """
        for name, _reason in _FIELD_OVERRIDES:
            declaring = [
                model for model in (ActivationGateVerdict, ExecutionGateVerdict) if name in self._declared_here(model)
            ]
            assert declaring, name
            for model in declaring:
                assert model.model_fields[name].description, f"{model.__name__}.{name}"

    def test_the_base_veto_list_is_not_implemented(self) -> None:
        bare = GateVerdictBase(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            confidence=0.9,
            n_resamples=99,
            rows_paired=8,
            rows_excluded=2,
            mean_diff=0.4,
            ci_low=0.1,
            ci_high=0.7,
            p_value=0.01,
        )
        with pytest.raises(NotImplementedError):
            _ = bare._own_vetoes
        with pytest.raises(NotImplementedError):
            _ = bare.failed_vetoes
        # `separated` needs no subclass, so it answers on the base.
        assert bare.separated is True

    def test_the_base_is_importable_from_coder_eval_models(self) -> None:
        import coder_eval.models as models

        assert models.GateVerdictBase is GateVerdictBase
        assert "GateVerdictBase" in models.__all__

    def test_extra_forbid_reaches_both_subclasses(self) -> None:
        for model in (ActivationGateVerdict, ExecutionGateVerdict):
            assert model.model_config.get("extra") == "forbid"


class TestFailedVetoes:
    """The ONE declaration of what vetoes a promotion, on both tracks.

    "It won and was vetoed" and "it lost" call for opposite next actions, and the set that separates
    them used to be spelled five times. A candidate whose sibling check failed rendered as the
    ordinary `NOT PROMOTED` headline because one of those five read `guardrails` alone.
    """

    @staticmethod
    def _check(name: str, *, passed: bool) -> GuardrailCheck:
        return GuardrailCheck(
            name=name, incumbent=1.0, candidate=1.0, relative_change=0.0, tolerance=0.25, passed=passed
        )

    @classmethod
    def _built(
        cls, track: str, *, own: list[GuardrailCheck], guardrails: list[GuardrailCheck]
    ) -> ActivationGateVerdict | ExecutionGateVerdict:
        """A verdict of either track carrying the two check lists given.

        Two literal branches rather than `copy_with(v, **{field_name: ...})`: a computed key defeats
        the whole reason that helper takes keywords, which is that a reader and a rename can see the
        field names in the source (CE048).
        """
        if track == "activation":
            return copy_with(full_activation_verdict(), sibling_checks=own, guardrails=guardrails)
        return copy_with(full_execution_verdict(), integrity_checks=own, guardrails=guardrails)

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_it_unions_both_lists_in_order(self, track: str) -> None:
        own_failed = [self._check("own", passed=False)]
        own_passed = [self._check("own", passed=True)]
        cost_failed = [self._check("cost (USD/row)", passed=False)]
        cost_passed = [self._check("cost (USD/row)", passed=True)]

        # Track-own list FIRST, then guardrails — the renderer joins this into a sentence.
        both = self._built(track, own=own_failed, guardrails=cost_failed)
        assert both.failed_vetoes == ["own", "cost (USD/row)"]

        own_only = self._built(track, own=own_failed, guardrails=cost_passed)
        assert own_only.failed_vetoes == ["own"]

        guardrail_only = self._built(track, own=own_passed, guardrails=cost_failed)
        assert guardrail_only.failed_vetoes == ["cost (USD/row)"]

        clean = self._built(track, own=own_passed, guardrails=cost_passed)
        assert clean.failed_vetoes == []
        # `not []` is True, which is the polarity every `promoted` conjunct depends on.
        assert not clean.failed_vetoes

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_both_properties_resolve_through_the_base(self, track: str) -> None:
        """One declaration means the attribute a track resolves IS the base's, not a twin of it.

        Asserted on the descriptor rather than on the answers: two identical copies agree on every
        input this class feeds them, which is exactly why the parity was maintainable by hand for so
        long and exactly why it drifted anyway.
        """
        verdict = self._built(track, own=[self._check("own", passed=False)], guardrails=[])
        model = type(verdict)
        assert model.failed_vetoes is GateVerdictBase.failed_vetoes
        assert model.separated is GateVerdictBase.separated
        # And the track's own list is what the base's union reaches for.
        assert verdict._own_vetoes == [self._check("own", passed=False)]
        assert verdict.failed_vetoes == ["own"]

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_an_unevaluable_check_is_absent_rather_than_a_third_state(self, track: str) -> None:
        # `passed` is a non-optional bool and an unevaluable check reports True with a note, so
        # there is nothing for `failed_vetoes` to handle — it simply does not appear.
        unevaluable = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=None,
            candidate=None,
            relative_change=None,
            tolerance=0.25,
            passed=True,
            note="not evaluated",
        )
        assert self._built(track, own=[], guardrails=[unevaluable]).failed_vetoes == []

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_it_is_not_persisted(self, track: str) -> None:
        """The fixture-stability guard: a `computed_field` would add a key to every pinned JSON."""
        build = full_activation_verdict if track == "activation" else full_execution_verdict
        assert "failed_vetoes" not in build().model_dump()
        assert "failed_vetoes" not in build().model_dump_json()

    @pytest.mark.parametrize("track", ["activation", "execution"])
    def test_a_duplicate_name_across_the_two_lists_appears_twice(self, track: str) -> None:
        # Preserved behaviour, not a fix: the collapse is a refactor.
        duplicated = self._built(
            track, own=[self._check("same", passed=False)], guardrails=[self._check("same", passed=False)]
        )
        assert duplicated.failed_vetoes == ["same", "same"]

    def test_the_polarity_survived_the_collapse_on_the_activation_track(self) -> None:
        """The activation site's old spelling was the POSITIVE `siblings_hold = all(...)`.

        The new one is the negative `not verdict.failed_vetoes`, and inverting a polarity during a
        collapse is the classic bug. Driven through `holm_promote` rather than asserted on the
        expression, so it is the DECISION being pinned.
        """
        verdict = copy_with(
            full_activation_verdict(),
            gate_refusal=None,
            promoted=None,
            sibling_checks=[self._check("sibling recall.yes [criterion 1]", passed=False)],
            guardrails=[self._check("cost (USD/row)", passed=True)],
        )
        decided = holm_promote([verdict])[0]
        assert decided.promoted is False, "a failing sibling check must veto"
        # And it WON its comparison, which is the distinction the veto exists to preserve.
        assert decided.holm_rejected is True
        assert decided.separated is True

        clean = copy_with(verdict, sibling_checks=[self._check("sibling recall.yes [criterion 1]", passed=True)])
        assert holm_promote([clean])[0].promoted is True, "nothing vetoed — the polarity is inverted"

    def test_the_polarity_survived_the_collapse_on_the_execution_track(self) -> None:
        verdict = copy_with(
            full_execution_verdict(),
            gate_refusal=None,
            promoted=None,
            integrity_checks=[self._check("engagement", passed=False)],
            guardrails=[self._check("cost (USD/row)", passed=True)],
        )
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is False
        assert decided.holm_rejected is True
        assert decided.separated is True

        clean = copy_with(verdict, integrity_checks=[self._check("engagement", passed=True)])
        assert holm_promote_execution([clean])[0].promoted is True

    def test_a_sibling_regression_still_gets_its_own_note(self) -> None:
        """`siblings_hold` must survive as a binding: the note ladder tells the two causes apart.

        Deleting it — which "replace the union sites" invites — makes a sibling regression print the
        generic guardrail note, and a reader then cannot tell "moved the failure" from "costs too
        much".
        """
        sibling_failed = copy_with(
            full_activation_verdict(),
            gate_refusal=None,
            promoted=None,
            sibling_checks=[self._check("sibling recall.yes [criterion 1]", passed=False)],
            guardrails=[self._check("cost (USD/row)", passed=True)],
        )
        notes = holm_promote([sibling_failed])[0].notes
        assert any("moved the failure rather than fixing it" in note for note in notes)
        # And the guardrail-naming note is NOT emitted for a sibling: that rung is guardrails-only,
        # because the sentence above is already the single declaration for a sibling failure.
        assert not any("sibling recall.yes [criterion 1]" in note for note in notes)

        guardrail_failed = copy_with(
            full_activation_verdict(),
            gate_refusal=None,
            promoted=None,
            sibling_checks=[self._check("sibling recall.yes [criterion 1]", passed=True)],
            guardrails=[self._check("cost (USD/row)", passed=False)],
        )
        guardrail_notes = holm_promote([guardrail_failed])[0].notes
        assert any("cost (USD/row)" in note for note in guardrail_notes)
        assert not any("moved the failure" in note for note in guardrail_notes)


def _full_confirm_verdict(execution: bool = True) -> ConfirmVerdict:
    """Every optional field set, so the round-trip check below proves something on each one."""
    return ConfirmVerdict(
        incumbent_variant="incumbent",
        candidate_variant="cand",
        suite_id=SUITE,
        train_effect=0.08,
        test_effect=0.075,
        test_mde=0.02,
        delta=-0.005,
        outcome="reproduced",
        test_verdict=full_execution_verdict() if execution else full_activation_verdict(),
        confirm_refusal="refused",
        notes=["a note"],
    )


class TestTypedConstruction:
    """A mistyped or renamed field must RAISE, not silently become None.

    Both verdicts used to be built through string-keyed dicts splatted into the constructor, and
    neither model nor `GuardrailCheck` declared `extra="forbid"` — so `mean_dif=0.42` produced a
    verdict reporting no difference at all, with every other number intact and nothing to see.
    """

    @pytest.mark.parametrize(
        ("build", "typo"),
        [
            (full_activation_verdict, "mean_dif"),
            (full_execution_verdict, "mean_dif"),
            (_full_confirm_verdict, "test_efect"),
            (full_guardrail_check, "relative_chnge"),
        ],
        ids=["activation", "execution", "confirm", "guardrail"],
    )
    def test_an_unknown_field_raises(self, build, typo: str) -> None:
        instance = build()
        model = type(instance)
        with pytest.raises(ValidationError):
            model(**{**instance.model_dump(), typo: 0.42})

    @pytest.mark.parametrize(
        "build",
        [full_activation_verdict, full_execution_verdict, _full_confirm_verdict, full_guardrail_check],
        ids=["activation", "execution", "confirm", "guardrail"],
    )
    def test_a_fully_populated_instance_round_trips(self, build) -> None:
        instance = build()
        model = type(instance)
        # Over `model_fields`, never a hand-picked subset: a field the construction rewrite dropped
        # is invisible to a comparison that never looks at it.
        dumped = instance.model_dump()
        assert model.model_validate(dumped) == instance
        # And through the NARROWER dump too. `exclude_unset=True` drops every field left at its
        # default, so it is the shape that would silently lose a newly-added stored field whose
        # writer forgot to set it — `holm_rejected` is exactly that shape on both verdicts.
        assert model.model_validate(instance.model_dump(exclude_unset=True)) == instance
        # And every OPTIONAL field is genuinely exercised. Compared against the real resolved
        # default — `field.get_default(call_default_factory=True)`, which covers `default=None` and
        # `default_factory=list` alike. Reading `field.default` instead excludes exactly those two,
        # which is almost every optional field on these models, and the guard then checks nothing.
        still_default = [
            name
            for name, field in model.model_fields.items()
            if not field.is_required() and dumped[name] == field.get_default(call_default_factory=True)
        ]
        assert not still_default, f"fixture leaves {still_default} at the default — it proves nothing there"


class TestConfirmVerdictCarriesEitherTrack:
    """`test_verdict` is a BARE `ActivationGateVerdict | ExecutionGateVerdict`, so pin what makes it work.

    Rubric item 12 wants a discriminated union, and there is no field to discriminate ON: neither
    verdict carries a track tag, and adding one would change two models that are already persisted in
    pinned fixtures. What makes the bare union safe instead is that both declare `extra="forbid"` and
    have DISJOINT required-plus-unique fields (`criterion_index` on one, `effect_size`/`dead_weight` on
    the other), so pydantic's smart mode cannot resolve either to the wrong member. That is an
    argument, and an argument about a persisted field needs a witness — a future field added to one
    model could make the two overlap, and the failure would be a Stage C block reporting the wrong
    track's verdict after a round-trip, silently.
    """

    @pytest.mark.parametrize("execution", [True, False], ids=["execution", "activation"])
    def test_the_concrete_member_survives_a_round_trip(self, execution: bool) -> None:
        verdict = _full_confirm_verdict(execution=execution)
        expected = ExecutionGateVerdict if execution else ActivationGateVerdict
        assert isinstance(verdict.test_verdict, expected)
        reloaded = ConfirmVerdict.model_validate_json(verdict.model_dump_json())
        assert isinstance(reloaded.test_verdict, expected)
        assert reloaded == verdict

    def test_the_two_members_have_no_common_shape(self) -> None:
        # The property the bare union rests on, asserted rather than assumed.
        activation = set(ActivationGateVerdict.model_fields)
        execution = set(ExecutionGateVerdict.model_fields)
        assert activation - execution and execution - activation, (
            "the two verdicts' field sets no longer differ in BOTH directions, so pydantic's smart "
            "mode could resolve `ConfirmVerdict.test_verdict` to the wrong member — give the union a "
            "discriminator, or a Stage C block will report the wrong track's numbers after a reload"
        )


class TestConfirmSplitCheckIsShared:
    """One split rule for both tracks, and it must not be an `if/elif` over the collapsed value.

    `SplitProvenance.value` collapses to `UNRECORDED_SPLIT` when ANY pooled dir is unreadable, so an
    `if unrecorded: note / elif value != "test": refuse` chain drops the refusal entirely for three
    dirs recording `train` beside one unreadable `run.json` — and the confirm then classifies over
    TRAIN rows carrying only a "provenance is missing from 1 of 4" note. That is precisely the failure
    the refusal exists for. The execution twin takes ONE run dir and cannot reach the state, which is
    exactly why the rule may not live on each track separately: the safe one would keep working while
    the other drifted.
    """

    @staticmethod
    def _provenance(*recorded: str | None, unrecorded: int = 0) -> SplitProvenance:
        return SplitProvenance(recorded=frozenset(recorded), unrecorded=unrecorded)

    def test_a_test_split_is_neither_refused_nor_noted(self) -> None:
        assert confirm_split_check(self._provenance("test"), [Path("d")]) == (None, None)

    def test_a_train_split_is_refused_with_the_re_run_remedy(self) -> None:
        refusal, note = confirm_split_check(self._provenance("train"), [Path("d")])
        assert refusal is not None and note is None
        assert "--split 'train'" in refusal and "reproduces by construction" in refusal

    def test_a_full_suite_selection_is_refused_too(self) -> None:
        # A recorded `None` means no `--split` was passed, so the confirm scored the train rows as well.
        refusal, _note = confirm_split_check(self._provenance(None), [Path("d")])
        assert refusal is not None and "held-out split" in refusal

    def test_an_unrecorded_split_alone_is_a_note(self) -> None:
        refusal, note = confirm_split_check(self._provenance(unrecorded=1), [Path("d")])
        assert refusal is None
        assert note is not None and "provenance is missing" in note

    def test_a_recorded_train_beside_an_unrecorded_dir_still_refuses(self) -> None:
        """THE regression this shared helper exists for — verified against the collapsed value.

        Three dirs recording `train` and one unreadable: `value` is `UNRECORDED_SPLIT`, so a chain
        keyed on it emits only the note. Both must appear.
        """
        provenance = self._provenance("train", unrecorded=1)
        assert provenance.value == UNRECORDED_SPLIT, "precondition: the value collapses"
        refusal, note = confirm_split_check(provenance, [Path("a"), Path("b")])
        assert refusal is not None and "--split 'train'" in refusal
        assert note is not None and "provenance is missing" in note

    def test_a_mixed_recording_names_every_off_split_value(self) -> None:
        refusal, _note = confirm_split_check(self._provenance("train", "holdout"), [Path("d")])
        assert refusal is not None and "'holdout'" in refusal and "'train'" in refusal

    @pytest.mark.parametrize("module", ["optimize.activation", "optimize.execution"])
    def test_neither_track_reimplements_the_rule(self, module: str) -> None:
        # The drift this closes was already present: the execution side had gained the
        # "not the Stage B winner" note that the activation side lacked, while the activation
        # docstring claimed both worked "for the reasons the execution twin's docstring gives".
        source = module_source(module)
        assert "confirm_split_check(" in source
        assert "reproduces by construction" not in source, f"{module} carries its own copy of the remedy"

    def test_the_activation_gate_refuses_a_train_beside_an_unrecorded_dir_end_to_end(self, tmp_path: Path) -> None:
        # Through the real gate, not only the helper: this track pools dirs per arm, so the state is
        # reachable there and nowhere else.
        inc, cand = confirm_activation_arms(tmp_path / "c", split="train", incumbent_hits=2, candidate_hits=5)
        (inc[0] / "run.json").unlink()
        t_inc, t_cand = confirm_activation_arms(tmp_path / "t", split="train", incumbent_hits=2, candidate_hits=5)
        train = holm_promote([activation_verdict_over_arms(t_inc, t_cand)])[0]
        confirm = confirm_gate(
            train_verdict=train,
            incumbent_run_dirs=inc,
            candidate_run_dirs=cand,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )
        assert confirm.confirm_refusal is not None and "--split 'train'" in confirm.confirm_refusal
        assert confirm.outcome == "undecided"
