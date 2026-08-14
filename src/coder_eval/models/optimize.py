"""Records produced by the `/coder-eval:optimize-skill` promotion gate.

Plain data, produced by :mod:`coder_eval.optimize_gate` rather than parsed from user input —
no discriminated union, no criterion type, nothing registered. They exist so the gate's verdict
is a typed value the skill prints, instead of arithmetic an agent performs by hand.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class GuardrailCheck(BaseModel):
    """One non-primary quantity that may veto a promotion.

    Covers both the sibling-regression checks (a candidate must not win by annexing another
    skill's requests) and the cost / latency guardrails. An unevaluable check reports
    ``passed=True`` with a ``note`` and ``None`` values — a missing measurement must never read
    as a pass on the merits, so the note is what the report prints instead of a number.
    """

    name: str = Field(description="What was checked, e.g. 'sibling recall.yes [criterion 1]' or 'cost'.")
    incumbent: float | None = Field(description="The incumbent arm's value, or None when it could not be measured.")
    candidate: float | None = Field(description="The candidate arm's value, or None when it could not be measured.")
    relative_change: float | None = Field(
        description="(candidate - incumbent) / incumbent. None when the incumbent value is zero or unmeasured."
    )
    tolerance: float = Field(
        description=(
            "How much movement is tolerated before the check fails. Units differ by check and the "
            "check's own name says which: cost/latency scale it by the incumbent's mean (a RELATIVE "
            "materiality floor), while a sibling recall check reads it as an ABSOLUTE drop in recall."
        )
    )
    ci_low: float | None = Field(
        default=None,
        description=(
            "Lower bound of the bootstrap interval on the arms' difference, where the check is "
            "bootstrap-derived. It is what the check fires on — even the optimistic end being a material "
            "increase — so reporting it shows WHY a guardrail did or did not fire, not only that it did."
        ),
    )
    ci_high: float | None = Field(default=None, description="Upper bound of that interval.")
    passed: bool = Field(description="False only on a measured breach; an unmeasurable check passes with a note.")
    note: str | None = Field(default=None, description="Why the check could not be evaluated, or what qualifies it.")


class ActivationGateVerdict(BaseModel):
    """The activation track's Stage B verdict for ONE candidate against the incumbent.

    Statistics are ``None`` rather than fabricated when the sample cannot support them (mirroring
    ``PairedComparison``), and ``rows_excluded`` is first-class so a silently narrowed sample is
    visible.

    ``promoted`` is deliberately ``bool | None``: a single gate cannot decide a family, so
    :func:`coder_eval.optimize_gate.activation_gate` leaves it ``None`` and only
    :func:`coder_eval.optimize_gate.holm_promote` — which sees every survivor at once — sets it.
    Rendering a ``None`` as a non-promotion would let a forgotten Holm pass look like an honest
    negative result.
    """

    incumbent_variant: str = Field(description="Variant id of the incumbent arm.")
    candidate_variant: str = Field(description="Variant id of the candidate arm under test.")
    suite_id: str = Field(description="Suite id (the pre-fan-out task_id) both arms ran.")
    criterion_index: int = Field(
        ge=0, description="Position of the gated criterion in the suite's success_criteria list (0-based)."
    )
    # Required, not defaulted: the gate always knows both, and a default here would be a second
    # declaration of values `activation_gate` already owns — which is how a report ends up
    # labelling a 90% interval as 95%.
    confidence: float = Field(
        gt=0.0, lt=1.0, description="Interval width the bootstrap used, so the report cannot mislabel it."
    )
    n_resamples: int = Field(
        gt=0, description="Bootstrap draws. It floors the p-value at 1/n, so a p AT that floor means below resolution."
    )
    rows_paired: int = Field(description="Rows scored on BOTH arms — the clusters the bootstrap resampled.")
    rows_excluded: int = Field(
        description=(
            "Rows seen in at least one arm but left out of the pairing: present on one side only, or "
            "present on both and scored on only one (an errored or timed-out row produces no criterion result)."
        )
    )
    incumbent_f1: float | None = Field(description="Incumbent's f1.yes pooled over the paired rows.")
    candidate_f1: float | None = Field(description="Candidate's f1.yes pooled over the paired rows.")
    mean_diff: float | None = Field(description="candidate_f1 - incumbent_f1, the bootstrap's point estimate.")
    ci_low: float | None = Field(description="Lower bound of the paired cluster-bootstrap interval on the difference.")
    ci_high: float | None = Field(
        description=(
            "Upper bound of that interval. Promotion requires the interval to exclude zero (ci_low > 0) "
            "AND the Holm-corrected test to reject; this bound is reported as the effect size rather "
            "than consulted, since a candidate's interval is bounded below by ci_low."
        )
    )
    p_value: float | None = Field(
        description="Two-sided bootstrap p, clamped below by the resample resolution. None when unpaired."
    )
    holm_alpha: float | None = Field(
        default=None, description="The family-wise alpha holm_promote applied. None until it has run."
    )
    promoted: bool | None = Field(
        default=None, description="None means gated but undecided — holm_promote has not been applied."
    )
    range_non_overlap: bool = Field(
        default=False,
        description=(
            "DIAGNOSTIC ONLY: min(candidate per-invocation F1) > max(incumbent's). The former gate, retained "
            "as a reported observation and never consulted in the promotion decision."
        ),
    )
    mde: float | None = Field(
        default=None,
        description="Minimum detectable effect for this suite at this size. None when it could not be computed.",
    )
    sibling_checks: list[GuardrailCheck] = Field(
        default_factory=list, description="Per-sibling recall.yes regression checks. A failure blocks promotion."
    )
    guardrails: list[GuardrailCheck] = Field(
        default_factory=list, description="Cost / latency guardrails. Advisory here, gating in the skill's prose."
    )
    notes: list[str] = Field(
        default_factory=list, description="Everything the reader needs to distrust or qualify the numbers above."
    )


class NoiseFloor(BaseModel):
    """A cached minimum detectable effect, keyed on everything that changes its value.

    **Every field above `mde` is part of the key**, because each one demonstrably moves the number:
    measured on one suite over the same 10 rows, the floor came out 0.402 at two invocations, 0.168
    at four and 0.000 at six. Key on a subset and a later round is served a floor from a different
    measurement — and since the floor decides whether a round runs at all, borrowing one either
    kills a real win or blesses an effect the suite cannot resolve. A miss just recomputes, which
    costs a bootstrap over data already on disk.

    `variant_id` is in the key for a mundane reason that makes it the likeliest trip: the incumbent
    variant is renamed round to round while suite, model and row count stay put. `seed` and
    `n_resamples` are in it for a smaller version of the same argument — they move the number by
    Monte-Carlo error rather than by a lot, but "every field above `mde` is part of the key" is
    only a rule worth having if it has no exceptions.
    """

    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1, description="Suite the floor was measured on (the pre-fan-out task_id).")
    variant_id: str = Field(min_length=1, description="Arm the null comparison split. Renamed per round, so keyed.")
    model: str = Field(
        min_length=1, description="Model the rows ran under. Sourced ONLY from optimize_gate.resolve_model."
    )
    criterion_index: int = Field(ge=0, description="Criterion position the floor was measured on.")
    n_rows: int = Field(ge=0, description="Rows scored in BOTH halves of the split — not the suite's row count.")
    n_invocations: int = Field(ge=0, description="Invocations the null comparison was split across.")
    confidence: float = Field(gt=0.0, lt=1.0, description="Interval width used. A wider interval is a wider floor.")
    seed: int = Field(description="Bootstrap seed. Two seeds give two (close, but different) floors.")
    n_resamples: int = Field(gt=0, description="Bootstrap draws. Fewer draws, coarser floor.")
    mde: float = Field(
        ge=0.0, le=1.0, description="The half-width: the smallest difference this suite can resolve. An F1 difference."
    )
    computed_at: AwareDatetime = Field(
        description="When it was measured, so a stale cache is legible rather than silent. Timezone-aware."
    )


class RegressionRow(BaseModel):
    """A row whose behaviour a past promotion justified, kept so a later round cannot silently undo it."""

    model_config = ConfigDict(extra="forbid")

    row_id: str = Field(description="Dataset row id. The corpus is de-duplicated on this.")
    promoted_in_round: int = Field(ge=0, description="Round whose promotion this row helped justify.")
    reason: str = Field(description="What this row demonstrated — why re-losing it would be a regression.")


class ArmRowScores(BaseModel):
    """One arm's score on each row — the vector a Pareto comparison needs.

    A suite-level average hides the shape: two candidates at the same mean can win on disjoint
    rows, which is a merge opportunity, or one can dominate outright, which is a discard. Only the
    per-row vector distinguishes them.
    """

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, description="The arm these scores belong to.")
    row_scores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Row id -> score, averaged across replicates the way paired_comparison averages before "
            "pairing. A row this arm produced no score for is ABSENT, never 0.0 — a hole is not a failure."
        ),
    )


class RoundScores(BaseModel):
    """One round's per-row vectors and the Pareto front computed from them.

    Kept so a later round can look back at which rows a discarded candidate actually won. That is
    the whole point of storing vectors rather than an average, so they are not truncated.
    """

    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=0, description="Round number, matching the ledger's numbering.")
    arm_row_scores: list[ArmRowScores] = Field(default_factory=list, description="One entry per arm.")
    pareto_front: list[str] = Field(
        default_factory=list, description="Variant ids not dominated on the row vector by any other arm."
    )


class OptimizeMeasurements(BaseModel):
    """The machine-read sidecar beside `history.json`: a noise-floor cache and a regression corpus.

    **Deliberately NOT the ledger.** `.optimize-skill/<skill>/history.json` stays free-form,
    append-only and agent-written, because its value is exactly the narrative a schema would have
    to reject — the superseded readings, the calibration notes, the record of why a four-way exact
    tie turned out to be total non-engagement rather than a ceiling. A model tight enough to
    validate that file would have made it unwritable in the first place. So the two things that
    genuinely need to be machine-read live here instead, and each file has one job.

    ``extra="forbid"`` is right here, where every field is machine-written: a typo must not become
    a permanent cache miss, and the regression corpus is not reconstructible. Pydantic does NOT
    propagate ``model_config`` into nested models, so :class:`NoiseFloor` and
    :class:`RegressionRow` declare it themselves — that is where the corpus actually lives.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(
        min_length=1, description="Skill these measurements belong to. Must match the parent directory name."
    )
    noise_floors: list[NoiseFloor] = Field(
        default_factory=list, description="Cache, replaced in place per (suite_id, model, n_rows)."
    )
    regression_corpus: list[RegressionRow] = Field(
        default_factory=list, description="Append-only, de-duplicated on row_id. Never rewritten."
    )
    round_scores: list[RoundScores] = Field(
        default_factory=list,
        description=(
            "Per-round row vectors and Pareto fronts. Measurements, so they belong here beside the "
            "noise floors rather than in the narrative ledger or in a third file."
        ),
    )
