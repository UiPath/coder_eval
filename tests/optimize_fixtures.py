"""Shared builders for the optimize family's tests: rows, run directories and verdicts.

A shared, non-numbered reader beside ``tests/lint/import_resolution.py``, ``cli_flags.py`` and
``markdown_tables.py``, which set the precedent for a module several test surfaces depend on.

**Why it exists.** ``tests/lint/computed_claims.py`` — a LINT-layer module — was importing private
helpers out of ``tests/test_optimize_gate.py``, which is an inverted dependency: the lint layer
reasons about the tree, and reaching into a test file for fixtures makes it depend on one. It is also
what blocked splitting that file, since three claim functions named its internals. The same applies
to ``tests/test_optimize_measurements.py``, which imported four of them.

**What lives here, DERIVED rather than chosen:** a builder referenced by more than one of the
per-module test files, plus everything the lint layer and the measurements suite import, plus the
transitive closure of both over builder-to-builder references. That last step is not optional —
``write_run_provenance`` and ``record_task_result`` are referenced by no test body at all, only by
``write_row``, so a grep over test bodies leaves them behind and breaks this module at import time.

Names here are PUBLIC. This is a shared module's surface, the same reasoning that took the underscore
off the optimize package's cross-module helpers (CE059). Two carry a qualifying noun instead of a
bare de-underscore, because the bare form already names a local somewhere in the suite:
``activation_verdict`` (``gate`` is 14 bindings across ``tests/``, one of them
``import coder_eval.optimize.gate as gate``) and ``arm_row_scores_for`` (``arm`` is 4). A name a
single target file uses stays with that file — three leak-preflight fixtures were moved here by an
over-wide first pass and moved back, because "derived" has to mean derived.

**It imports ``coder_eval`` and stdlib only.** Never ``tests.lint`` and never a ``test_*`` module —
that would re-create the inversion this exists to undo, and a test asserts it.

Builders taking a path take it as an explicit argument rather than reaching for ``tmp_path``, because
``computed_claims.py`` calls them from outside a test.
"""

import json
from datetime import datetime
from pathlib import Path

from coder_eval.models import (
    ActivationGateVerdict,
    ArmRowScores,
    ClassificationCriterionResult,
    CriterionResult,
    EvaluationResult,
    ExecutionGateVerdict,
    ExperimentResult,
    FinalStatus,
    GuardrailCheck,
    NoiseFloor,
    RowSelection,
    RunSummary,
    TokenUsage,
)
from coder_eval.optimize.activation import activation_gate
from coder_eval.optimize.execution import execution_gate, measure_execution_noise_floor
from coder_eval.optimize.gate import MATERIALITY_FLOOR
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES


SUITE = "my-skill-activation"


def eval_result(row_id: str, labels: list[tuple[str, str]], *, extra_basic: bool = False) -> EvaluationResult:
    """One replicate's result carrying one classification result per (expected, observed) pair.

    Descriptions interpolate the row id, exactly as both bundled templates do — which is what
    makes a description-keyed implementation impossible and the positional one necessary.
    """
    results: list[CriterionResult] = [
        ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description=f"criterion {i} for row {row_id}",
            score=1.0 if expected == observed else 0.0,
            expected_label=expected,
            observed_label=observed,
        )
        for i, (expected, observed) in enumerate(labels)
    ]
    if extra_basic:
        results.append(CriterionResult(criterion_type="file_check", description=f"file for row {row_id}", score=1.0))
    return EvaluationResult(
        task_id=f"{SUITE}/{row_id}",
        task_description="row",
        agent_type="claude-code",
        started_at=datetime(2026, 8, 13, 12, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        success_criteria_results=results,  # type: ignore[arg-type]
    )


# The run.json key the gate reads, taken from the model that declares it rather than typed again.
RUN_SELECTION_KEY = next(name for name in RunSummary.model_fields if name == "row_selection")


def write_run_provenance(run_dir: Path, split: str | None = None) -> None:
    """Stamp a minimal ``run.json`` carrying the run's row selection.

    Every arm builder goes through ``write_row``, which calls this, so fixtures model a run
    directory written by a CURRENT coder-eval rather than a pre-provenance one. Without it every
    fixture takes the "unrecorded" path: the gate would note it on every block (churning six
    pinned renders) and — the part that actually bites — ``record_noise_floor`` would refuse to
    cache any floor, since a floor measured over runs whose row sets are unknown is a floor for
    no particular row set. The unrecorded and mismatched paths get their own explicit tests
    instead of contaminating every other one.

    Only the keys the gate reads are written; ``RunSummary`` has many more, and a fixture that
    tracked all of them would be a second implementation of the writer.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run.json"
    if path.exists():
        return
    # Built from the REAL models rather than hand-typed keys. `read_split_provenance` hand-writes
    # its READER, and this hand-wrote its WRITER — so renaming `RunSummary.row_selection` or
    # `RowSelection.split` would have moved both in lockstep, leaving every test green while
    # production went 100% "unrecorded". Deriving the payload here is what breaks that symmetry.
    payload = {RUN_SELECTION_KEY: RowSelection(split=split).model_dump(mode="json"), "task_results": []}
    path.write_text(json.dumps(payload), encoding="utf-8")


def record_task_result(run_dir: Path, variant: str, task_id: str, replicate: int) -> None:
    """Append one executed row to ``run.json``'s ``task_results``, as a real run does.

    The tree-reconciliation preflight asks whether run.json describes the rows on disk, so a
    fixture that writes rows without recording them models a CONTAMINATED run dir. Every builder
    goes through ``write_row``, which calls this, so the ordinary fixture is a clean one and the
    contaminated case is built deliberately (``write_row(..., record=False)``).

    Only the three keys the reconciliation reads, on the same "do not re-implement the writer"
    grounds as ``write_run_provenance`` above. ``replicate_index`` is load-bearing: the
    reconciliation keys on ``(row, replicate)``, so a fixture omitting it would send every entry
    down the permissive whole-row path and leave the replicate half of the check untested.
    """
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("task_results", []).append(
        {"task_id": task_id, "variant_id": variant, "replicate_index": replicate}
    )
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_row(
    run_dir: Path,
    variant: str,
    row_id: str,
    result: EvaluationResult,
    replicate: int = 0,
    *,
    record: bool = True,
) -> Path:
    """Write one replicate's ``task.json``, recording it in ``run.json`` unless told not to.

    ``record=False`` writes the row to disk WITHOUT recording it — a row left behind by an earlier
    invocation of a re-used ``--run-dir``, which is exactly what the tree-reconciliation preflight
    refuses on.
    """
    task_dir = run_dir / variant / SUITE / row_id / f"{replicate:02d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "task.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    write_run_provenance(run_dir)
    if record:
        record_task_result(run_dir, variant, f"{SUITE}/{row_id}", replicate)
    return path


def write_arm(
    tmp_path: Path,
    variant: str,
    per_row_labels: dict[str, list[tuple[str, str]]],
    *,
    invocations: int = 3,
    prefix: str = "",
) -> list[Path]:
    """One arm across ``invocations`` separate run directories — Stage B's three invocations."""
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"{prefix}run-{i}"
        for row_id, labels in per_row_labels.items():
            write_row(run_dir, variant, row_id, eval_result(row_id, labels))
        run_dirs.append(run_dir)
    return run_dirs


def shared_dirs(
    tmp_path: Path,
    incumbent: dict[str, list[tuple[str, str]]],
    candidate: dict[str, list[tuple[str, str]]],
    *,
    invocations: int = 3,
) -> list[Path]:
    """Both arms written into the SAME run directories, as a real experiment produces them."""
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, labels in incumbent.items():
            write_row(run_dir, "incumbent", row_id, eval_result(row_id, labels))
        for row_id, labels in candidate.items():
            write_row(run_dir, "candidate", row_id, eval_result(row_id, labels))
        run_dirs.append(run_dir)
    return run_dirs


# The gate's real default is GATE_RESAMPLES (20,000), which is what a promotion decision needs and
# what ~80 tests in this file do NOT: at four bootstraps per call it would take the file from ~3s to
# ~35s. So the helper passes a small count, resample-sensitive tests pass their own, and
# `test_gate_defaults_to_the_gate_resample_count` is the one test that exercises the signature.
FAST_RESAMPLES = 400


def activation_verdict(run_dirs: list[Path], **kwargs) -> ActivationGateVerdict:
    return activation_gate(
        incumbent_run_dirs=run_dirs,
        candidate_run_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=SUITE,
        **{"criterion_index": 0, "n_resamples": FAST_RESAMPLES, **kwargs},
    )


def module_path(module: str) -> Path:
    """The file behind a family module name.

    Names are DOTTED and package-relative to `coder_eval` — `"optimize.load"`, never `"load"` and
    never the pre-package `"optimize_load"` — which is what keeps the rank test's
    `imported.removeprefix("coder_eval.")` comparable with `_OPTIMIZE_RANKS`'s keys. The one flat
    sibling, `"reports_optimize"`, splits into a single component and resolves at the top level.
    """
    return Path(__file__).parent.parent.joinpath("src", "coder_eval", *module.split(".")).with_suffix(".py")


def module_source(module: str) -> str:
    return module_path(module).read_text(encoding="utf-8")


def costed_result(
    row_id: str, labels: list[tuple[str, str]], *, cost: float | None, duration: float
) -> EvaluationResult:
    """A row result carrying cost and duration, for the guardrail tests."""
    result = eval_result(row_id, labels)
    return result.model_copy(
        update={
            "duration_seconds": duration,
            "total_token_usage": TokenUsage(total_cost_usd=cost) if cost is not None else None,
        }
    )


def cost_rows(per_row: dict[str, list[float]], *, duration: float = 10.0) -> dict[str, list[EvaluationResult]]:
    return {
        rid: [costed_result(rid, [("yes", "yes")], cost=c, duration=duration) for c in costs]
        for rid, costs in per_row.items()
    }


def cost_check(checks: list[GuardrailCheck]) -> GuardrailCheck:
    return next(c for c in checks if c.name.startswith("cost"))


def headline_line(text: str) -> str:
    """The rendered block's headline, unwrapped from its bold markers.

    Named for what it does — it EXTRACTS a line from rendered markdown. Not to be confused with
    ``reports_optimize._headline``, which BUILDS the string this reads back; assertions here read
    ``headline_line(render_markdown(...))``, and one name for both jobs made that line lie.

    Reading the headline LINE rather than asserting a substring is absent from the whole block:
    "PROMOTED" is a substring of "NOT PROMOTED", so a `not in` over the block is either wrong or a
    no-op depending on the fixture, and both failure modes already exist in this file.

    It got sharper once the failed-check note began quoting the headline's own words ("the rendered
    headline reports it as BLOCKED BY A GUARDRAIL"): `"BLOCKED" not in block` now matches that
    SENTENCE and passes on a block whose headline is something else entirely. Assert on the
    discriminating line, never on a substring of the whole page.
    """
    return next(line for line in text.splitlines() if line.startswith("**")).strip("*")


def failing_cost_check() -> GuardrailCheck:
    """A measured, material cost breach — the check both tracks must now veto on."""
    return GuardrailCheck(
        name="cost (USD/row)",
        incumbent=1.0,
        candidate=2.0,
        relative_change=1.0,
        tolerance=MATERIALITY_FLOOR,
        ci_low=0.6,
        ci_high=1.4,
        passed=False,
    )


def parity_activation(**overrides) -> ActivationGateVerdict:
    """A separating activation verdict, ready for `holm_promote`."""
    base = {
        "incumbent_variant": "incumbent",
        "candidate_variant": "cand",
        "suite_id": SUITE,
        "criterion_index": 0,
        "confidence": 0.95,
        "n_resamples": BOOTSTRAP_RESAMPLES,
        "rows_paired": 12,
        "rows_excluded": 0,
        "incumbent_f1": 0.4,
        "candidate_f1": 0.9,
        "mean_diff": 0.5,
        "ci_low": 0.2,
        "ci_high": 0.75,
        "p_value": 0.001,
    }
    return ActivationGateVerdict(**{**base, **overrides})


def parity_execution(**overrides) -> ExecutionGateVerdict:
    """The execution twin of `parity_activation`, on the same numbers."""
    base = {
        "incumbent_variant": "incumbent",
        "candidate_variant": "cand",
        "suite_id": EXEC_SUITE,
        "confidence": 0.95,
        "n_resamples": BOOTSTRAP_RESAMPLES,
        "rows_paired": 12,
        "rows_excluded": 0,
        "mean_diff": 0.5,
        "ci_low": 0.2,
        "ci_high": 0.75,
        "effect_size": 1.1,
        "p_value": 0.001,
    }
    return ExecutionGateVerdict(**{**base, **overrides})


def scored_result(row_id: str, score: float) -> EvaluationResult:
    """A row result carrying a weighted_score plus one criterion scoring the same."""
    return eval_result(row_id, [("yes", "yes" if score >= 0.5 else "no")]).model_copy(update={"weighted_score": score})


def tiny_suite(positives: int, distractors: int) -> tuple[dict, dict]:
    """A suite the incumbent misses entirely and the candidate gets perfect, plus shared distractors."""
    incumbent = {f"p{i}": [("yes", "no")] for i in range(positives)}
    candidate = {f"p{i}": [("yes", "yes")] for i in range(positives)}
    for i in range(distractors):
        incumbent[f"d{i}"] = [("no", "no")]
        candidate[f"d{i}"] = [("no", "no")]
    return incumbent, candidate


def weighted_arm(tmp_path: Path, variant: str, per_row: dict[str, list[float]], *, run_dirs: int = 1) -> list[Path]:
    """One arm whose rows carry N replicates each, spread across ``run_dirs`` directories.

    `weighted_score` is set EXPLICITLY on every result: it is `float | None` with default None,
    populated by the orchestrator, so a fixture that forgets it makes every cluster empty and the
    floor silently comes back None.
    """
    dirs = [tmp_path / f"run-{i}" for i in range(run_dirs)]
    for row_id, scores in per_row.items():
        for i, score in enumerate(scores):
            run_dir = dirs[i % run_dirs]
            replicate = i // run_dirs
            write_row(run_dir, variant, row_id, scored_result(row_id, score), replicate)
    return dirs


def execution_floor(run_dirs: list[Path], **kwargs) -> NoiseFloor | None:
    return measure_execution_noise_floor(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        **{"model": "claude-haiku-4-5", "n_resamples": FAST_RESAMPLES, **kwargs},
    )


def cost_quality_arm(tmp_path: Path, variant: str, per_row: dict[str, tuple[float, float | None]]) -> Path:
    """One arm's rows as (weighted_score, cost) pairs. A None cost records no cost at all."""
    run_dir = tmp_path / "run-0"
    for row_id, (score, cost) in per_row.items():
        result = costed_result(row_id, [("yes", "yes")], cost=cost, duration=10.0)
        write_row(run_dir, variant, row_id, result.model_copy(update={"weighted_score": score}))
    return run_dir


# The real vector from `runs/baseline-3` (Sonnet, 15 rows) and the rule attribution measured with
# it, NOT a fixture reverse-engineered to hit the answer. It is the round the ceiling was derived
# from, and `tests/lint/computed_claims.py` imports both names to check the skill's Step 7 table
# against the code — so the fixture, the function and the prose cannot drift apart in pairs.
HEADROOM_ROW_SCORES = {
    "budget-variance": 1.000,
    "clean-inventory": 1.000,
    "cumulative-revenue": 1.000,
    "dept-cost-ranking": 0.857,
    "extend-existing-budget": 0.857,
    "growth-projection": 0.833,
    "loaded-cost": 0.857,
    "opex-ratio": 1.000,
    "order-value-lookup": 1.000,
    "region-product-pivot": 1.000,
    "sales-summary": 1.000,
    "sku-labels": 0.750,
    "tier-classification": 1.000,
    "top-regions": 0.800,
    "two-sheet-model": 0.833,
}
HEADROOM_RULE_ROWS = {
    "R1": {"sku-labels", "top-regions"},
    "R6": {"growth-projection", "two-sheet-model"},
    "R7": {"dept-cost-ranking", "loaded-cost"},
    "R8": {"extend-existing-budget"},
}
# The floor that round measured, and what the ceilings were compared against.
HEADROOM_FLOOR = 0.0255

EXEC_SUITE = SUITE


def experiment_json(run_dir: Path, variant_ids: list[str], per_replicate: dict[str, dict[str, list[float]]]) -> Path:
    """A minimal two-variant `experiment.json`, written where a real run writes it.

    Built through `ExperimentResult` rather than as a hand-rolled dict, so a field the model
    requires cannot be forgotten here and pass anyway.
    """
    result = ExperimentResult(
        experiment_id="round1-gate",
        description="gate",
        variant_ids=variant_ids,
        task_summaries=[],
        variant_aggregates={},
        total_duration_seconds=1.0,
        per_replicate_scores=per_replicate,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "experiment.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    return path


def exec_run_dir(
    tmp_path: Path,
    *,
    incumbent: dict[str, list[float]],
    candidate: dict[str, list[float]],
    declare_incumbent_first: bool = True,
    extra_scores: dict[str, dict[str, list[float]]] | None = None,
    variant_ids: list[str] | None = None,
) -> Path:
    """One Stage B gate run directory: both arms' rows on disk plus the experiment file.

    The directory name is FIXED, so two calls under one ``tmp_path`` would write into the same tree
    — the second arm's rows landing beside the first's while `experiment.json` is overwritten. That
    is silent and it drifts a fixture without failing anything: measured, a test comparing a refused
    fixture against a clean control ran the control over the refused arm's leftover rows and moved
    its `mde` from 2.8e-17 to 0.030, with the assertion still green. Refusing to build twice is what
    turns that into a test error; the fix at a call site is a distinct `tmp_path / "<name>"`.
    """
    run_dir = tmp_path / "round1-gate"
    assert not run_dir.exists(), (
        f"{run_dir} already exists — two exec_run_dir calls under one tmp_path merge silently. "
        "Give each fixture its own subdirectory, e.g. exec_run_dir(tmp_path / 'winner', ...)."
    )
    for variant, per_row in (("incumbent", incumbent), ("candidate", candidate)):
        for row_id, scores in per_row.items():
            for replicate, score in enumerate(scores):
                write_row(run_dir, variant, row_id, scored_result(row_id, score), replicate)

    per_replicate = {
        variant: {f"{EXEC_SUITE}/{row_id}": list(scores) for row_id, scores in per_row.items()}
        for variant, per_row in (("incumbent", incumbent), ("candidate", candidate))
    }
    for variant, extra in (extra_scores or {}).items():
        per_replicate.setdefault(variant, {}).update(extra)
    declared = variant_ids or (["incumbent", "candidate"] if declare_incumbent_first else ["candidate", "incumbent"])
    experiment_json(run_dir, declared, per_replicate)
    return run_dir


def exec_gate(run_dir: Path, **kwargs) -> ExecutionGateVerdict:
    return execution_gate(
        run_dir=run_dir,
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=EXEC_SUITE,
        **{"n_resamples": FAST_RESAMPLES, **kwargs},
    )


# A candidate that wins on every row, with within-row spread so the paired t has variance.
WINNER = {
    "incumbent": {"r1": [0.2, 0.3], "r2": [0.4, 0.5], "r3": [0.1, 0.2], "r4": [0.5, 0.6]},
    "candidate": {"r1": [0.7, 0.8], "r2": [0.9, 1.0], "r3": [0.6, 0.8], "r4": [0.9, 0.9]},
}


def uniform_shift(n_rows: int, *, shift: float = 0.5) -> dict[str, dict[str, list[float]]]:
    """Two flat arms differing by an IDENTICAL amount on every row — zero paired variance.

    The shape the shipped `outcome-rows.jsonl` train split produces: per-row `weighted_score` is a
    weighted mean over a handful of discrete criterion scores, so identical per-row differences are
    ordinary rather than exotic. The paired t then reports p = 0.0000 with a zero-width interval.
    """
    return {
        "incumbent": {f"r{i}": [0.5, 0.5] for i in range(n_rows)},
        "candidate": {f"r{i}": [round(0.5 + shift, 3)] * 2 for i in range(n_rows)},
    }


def confirm_dir(tmp_path: Path, *, split: str | None, **arms) -> Path:
    """A gate run dir whose `run.json` records a given `--split`.

    The split is rewritten AFTER the rows go down, not before: `exec_run_dir` refuses to build into
    an existing directory (two calls under one tmp_path merge silently), and `write_row` creates
    `run.json` on the first row. So `row_selection` is patched in place, leaving `task_results` — which
    the tree reconciliation reads — exactly as the row writer left it.
    """
    run_dir = exec_run_dir(tmp_path, **(arms or WINNER))
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[RUN_SELECTION_KEY] = RowSelection(split=split).model_dump(mode="json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return run_dir


def pinned_suite() -> tuple[dict, dict]:
    """`tiny_suite` plus a SIBLING criterion the candidate annexes one row of.

    Load-bearing for the pins rather than decoration. `tiny_suite`'s rows carry one criterion, so
    `sibling_checks` comes back `[]` — which is the model default, so a construction that dropped
    the `sibling_checks=` keyword entirely would reproduce the pin byte for byte. Measured: with a
    single-criterion suite, deleting `sibling_checks=` from both return paths left the whole file
    green. It is the field `holm_promote` folds into `promoted`, so it is the last one a
    behaviour pin may be blind to.
    """
    incumbent, candidate = tiny_suite(4, 4)
    return (
        {rid: [*labels, ("yes", "yes")] for rid, labels in incumbent.items()},
        {rid: [*labels, ("yes", "no" if rid == "p0" else "yes")] for rid, labels in candidate.items()},
    )


def full_guardrail_check() -> GuardrailCheck:
    """A `GuardrailCheck` with every field set away from its default."""
    return GuardrailCheck(
        name="cost (USD/row)",
        incumbent=1.0,
        candidate=2.0,
        relative_change=1.0,
        tolerance=0.25,
        ci_low=0.5,
        ci_high=1.5,
        rate=0.25,
        passed=False,
        note="a note",
    )


def full_activation_verdict() -> ActivationGateVerdict:
    return ActivationGateVerdict(
        incumbent_variant="incumbent",
        candidate_variant="cand",
        suite_id=SUITE,
        criterion_index=1,
        confidence=0.9,
        n_resamples=99,
        rows_paired=8,
        rows_excluded=2,
        incumbent_f1=0.4,
        candidate_f1=0.8,
        mean_diff=0.4,
        ci_low=0.1,
        ci_high=0.7,
        p_value=0.01,
        p_floor=0.005,
        n_discordant=4,
        gate_refusal="refused",
        holm_alpha=0.05,
        holm_rejected=True,  # rejected at its rank, yet not promoted — the refusal is what vetoed
        # False, not True: a refusal forces `promoted=False`, and a carrier fixture that pairs a
        # refusal with a promotion is a state no gate can emit.
        promoted=False,
        range_non_overlap=True,
        mde=0.2,
        sibling_checks=[full_guardrail_check()],
        guardrails=[full_guardrail_check()],
        notes=["a note"],
    )


def full_execution_verdict() -> ExecutionGateVerdict:
    return ExecutionGateVerdict(
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
        effect_size=1.2,
        p_value=0.01,
        gate_refusal="refused",
        holm_alpha=0.05,
        holm_rejected=True,  # rejected at its rank, yet not promoted — the refusal is what vetoed
        promoted=False,  # as above: a refusal forces this False
        mde=0.2,
        dead_weight=0.5,
        primary_criterion_index=1,
        primary_mean_diff=0.9,
        integrity_checks=[full_guardrail_check()],
        guardrails=[full_guardrail_check()],
        notes=["a note"],
    )


def arm_row_scores_for(variant: str, scores: dict[str, float]) -> ArmRowScores:
    return ArmRowScores(variant_id=variant, row_scores=scores)


def set_split(run_dir: Path, split: str | None) -> None:
    """Overwrite a fixture run dir's recorded split (``write_row`` stamps `None` by default)."""
    (run_dir / "run.json").write_text(
        json.dumps({"row_selection": {"split": split, "max_rows": None, "sample_per_stratum": None}}),
        encoding="utf-8",
    )
