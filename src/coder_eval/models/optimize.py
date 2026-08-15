"""Records produced by the `/coder-eval:optimize-skill` promotion gate.

Plain data, produced by :mod:`coder_eval.optimize_gate` rather than parsed from user input —
no discriminated union, no criterion type, nothing registered. They exist so the gate's verdict
is a typed value the skill prints, instead of arithmetic an agent performs by hand.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


# The label whose F1 the activation gate reads. `skill_triggered` emits `yes` / `no`, and
# "did the skill engage when it should" is the `yes` class.
#
# It lives HERE rather than in `optimize_gate` because `NoiseFloor.metric`'s default interpolates
# it, and this module cannot import the gate — the gate imports these models, and the reverse is a
# cycle. Same cycle-free-leaf role `models/judge_defaults.py` plays for `DEFAULT_JUDGE_MODEL`.
TARGET_LABEL = "yes"


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
            "The bar this check is decided against. Its meaning differs by check and the check's "
            "own name says which: cost/latency scale it by the incumbent's mean (a RELATIVE "
            "materiality floor), a sibling recall check reads it as an ABSOLUTE permitted drop, and "
            "the execution track's engagement check reads it as an ABSOLUTE FLOOR the candidate "
            "must reach (1.0 — a row the skill never engaged on is not evidence about its body, "
            "however the incumbent did on it), not as a permitted movement."
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
    rate: float | None = Field(
        default=None,
        description=(
            "An optional SECOND reading for a check that has one, in the same spirit as "
            "relative_change beside it — the check's own `name` says what it means. Today only the "
            "sibling checks set it, to the ANNEXATION rate: of the sibling's true-yes rows, the "
            "fraction the candidate turned into 'no' that the incumbent did not. It is a reading, "
            "never a second gate — `passed` is decided by the recall drop alone. None wherever "
            "there is no such quantity, including a sibling with no true instances to annex."
        ),
    )
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
        gt=0,
        description=(
            "Bootstrap draws. The estimator's own floor is 2/(n+1) — see reports_stats."
            "bootstrap_p_floor — so a p AT that floor is a resolution statement, not a measurement. "
            "This suite's own discreteness floor sits above it; p_floor is the one that decides."
        ),
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
        description=(
            "Two-sided bootstrap p under the Phipson & Smyth (b+1)/(m+1) correction, so it is "
            "floored at 2/(n_resamples+1) by the estimator itself rather than by a clamp — and "
            "floored again, higher, by what this suite's discordant-row count can express "
            "(p_floor). None when unpaired."
        )
    )
    p_floor: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        # Bounded like NoiseFloor.mde beside it, and for a sharper reason than tidiness: the
        # refusal fires on `p_floor > threshold`, and NaN > anything is False — so a non-finite
        # value would silently DISABLE the gate's refusal rather than failing loudly.
        description=(
            "The smallest two-sided p this suite at this size can express: 2*(1-R/M)^M for M paired "
            "rows of which R are discordant, floored at the bootstrap's own 2/(n_resamples+1). None "
            "when there was no interval. Compared against the Holm threshold by holm_promote — where "
            "it exceeds it, no candidate can promote however good it is."
        ),
    )
    n_discordant: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Paired rows whose two arms produced different pooled label multisets — the R that "
            "p_floor is computed from, and the quantity a refusal's remedy turns on. None when "
            "there was no interval; 0 is the meaningful 'the arms agreed everywhere'. It is the "
            "COUNT that binds, not the rate, and the count is almost flat in the suite size: 3 "
            "discordant rows suffice at 8 paired rows and 4 at 10, 20 or 100 (see "
            "optimize_gate.min_discordant_rows, which computes it). What does NOT help is adding "
            "rows the arms agree on — at a fixed R that RAISES the floor."
        ),
    )
    gate_refusal: str | None = Field(
        default=None,
        description=(
            "Set by holm_promote when the suite's discreteness floor exceeds this candidate's Holm "
            "threshold, i.e. the gate structurally CANNOT separate. Renders as its own headline: a "
            "refusal is not a negative result, and reporting it as one is the defect this field fixes."
        ),
    )
    holm_alpha: float | None = Field(
        default=None, description="The family-wise alpha holm_promote applied. None until it has run."
    )
    promoted: bool | None = Field(
        default=None,
        description=(
            "None means gated but undecided — holm_promote has not been applied. It folds the "
            "SIBLING checks in (a candidate that moved the failure has not fixed it) but leaves the "
            "cost/latency guardrails advisory, which the skill's prose gates on. Note this differs "
            "from ExecutionGateVerdict.promoted, which folds in neither — so do not carry a habit "
            "from one gate to the other; read each field's own description."
        ),
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


class ExecutionGateVerdict(BaseModel):
    """The execution track's Stage B verdict for ONE candidate against the incumbent.

    Mirrors :class:`ActivationGateVerdict`'s conventions deliberately — statistics are ``None``
    rather than fabricated, ``notes`` is the distrust-the-numbers channel, and ``promoted`` is
    ``None`` until :func:`coder_eval.optimize_gate.holm_promote_execution` has seen the whole
    family — but it is a separate flat model rather than a track-discriminated union with it. A
    union would carry ``p_floor`` / ``n_discordant`` / ``criterion_index`` as permanently-``None``
    noise on one side and ``effect_size`` on the other, and every reader would have to know which
    half applied. (``gate_refusal`` is deliberately NOT in that list: both tracks refuse, so it
    carries the same name and the same meaning here — a different condition, set by a different
    function, for reasons its own description gives.)

    **``mean_diff`` is ALWAYS candidate - incumbent.** The reporter's own ``## Paired Comparison``
    block subtracts in variant *declaration* order, so with the incumbent declared first a
    candidate win renders there as a negative number — which the method file warns about twice,
    because a reversed reading promotes the arm that lost. The gate knows which arm is the
    incumbent, so it resolves the sign once, in code, and every number here reads the same way
    regardless of how the experiment file was written. ``ci_low``/``ci_high`` are swapped along
    with it, so ``ci_low <= ci_high`` always holds.

    **There is no ``p_floor`` here, and that is not an oversight.** The activation gate's
    discreteness floor exists because a resample that draws no discordant row produces a difference
    of exactly 0.0, which bounds the smallest p a suite can express. The paired *t* is continuous
    and has no such floor. **It does, however, have a degenerate case** — per-row ``weighted_score``
    is a weighted mean over a handful of discrete criterion scores, so two arms can differ by an
    identical amount on every row. The paired *t* then reports p = 0.0000 with a zero-width interval
    and every promotion conjunct holds at once. ``gate_refusal`` reports that (and the zero-row
    wiring fault beside it), and ``promoted`` is False whenever it is set.
    """

    incumbent_variant: str = Field(description="Variant id of the incumbent arm.")
    candidate_variant: str = Field(description="Variant id of the candidate arm under test.")
    suite_id: str = Field(description="Suite id (the pre-fan-out task_id) both arms ran.")
    confidence: float = Field(
        gt=0.0, lt=1.0, description="Interval width the paired t used, so the report cannot mislabel it."
    )
    n_resamples: int = Field(
        gt=0,
        description=(
            "Bootstrap draws used for the MDE and the cost/latency guardrails this verdict carries "
            "— NOT for the primary statistic, which is an analytic paired t. Recorded for the same "
            "reason ActivationGateVerdict records it: without it the same block can be produced at "
            "two very different resolutions and read identically."
        ),
    )
    rows_paired: int = Field(description="Rows scored by BOTH arms — PairedComparison.task_count.")
    rows_excluded: int = Field(
        description=(
            "Rows seen for at least one arm but not paired, carried through from "
            "PairedComparison.excluded_count rather than recomputed."
        )
    )
    mean_diff: float | None = Field(
        default=None,
        description="Paired mean difference in per-row weighted_score, ALWAYS candidate - incumbent.",
    )
    ci_low: float | None = Field(default=None, description="Lower bound of the paired-t interval on that difference.")
    ci_high: float | None = Field(default=None, description="Upper bound of that interval.")
    effect_size: float | None = Field(
        default=None,
        description=(
            "Cohen's d for the paired difference. None does NOT mean the comparison failed — d is "
            "undefined at zero variance, which two arms agreeing exactly on every row produce."
        ),
    )
    p_value: float | None = Field(default=None, description="Paired t-test p. None when fewer than 2 rows paired.")
    gate_refusal: str | None = Field(
        default=None,
        description=(
            "Why this block is NOT a decision, or None when it is one. Set by execution_gate for "
            "either of two causes: an arm that loaded ZERO rows (a wiring fault — this track's "
            "statistic comes from experiment.json rather than from the rows, so it can look fine "
            "while every check reads green over nothing), or paired differences carrying ZERO "
            "variance (the two arms differed by an identical amount on every row, so the paired t "
            "reports p = 0.0 with a zero-width interval and every promotion conjunct holds at once "
            "on a sample that separated nothing). One field for both because they answer the same "
            "question with the same consequence; the message names which cause and its remedy, "
            "which differ. holm_promote_execution forces promoted=False whenever it is set. Note "
            "the DIFFERENT setter from ActivationGateVerdict.gate_refusal, which holm_promote sets "
            "because a discreteness refusal needs the family's rank-dependent threshold; both "
            "causes here need nothing outside a single verdict, so each is detected where it is "
            "already computed."
        ),
    )
    holm_alpha: float | None = Field(
        default=None, description="The family-wise alpha holm_promote_execution applied. None until it has run."
    )
    promoted: bool | None = Field(
        default=None,
        description=(
            "None means gated but undecided — holm_promote_execution has not been applied. "
            "**It reports the PRIMARY statistic's decision only**: Holm rejecting, the difference "
            "favouring the candidate, and the interval excluding zero — plus `gate_refusal` being "
            "unset, which is not a fourth criterion so much as the statement that those three mean "
            "anything at all. Guardrails and integrity "
            "checks do NOT enter it — they gate in render_execution_markdown, which headlines "
            "BLOCKED BY A GUARDRAIL over a True promoted. So never ship on this field alone: read "
            "the rendered block, or check `all(c.passed for c in (*integrity_checks, *guardrails))` "
            "beside it. (This differs from ActivationGateVerdict, where sibling checks are folded "
            "into `promoted` and only the cost/latency guardrails stay advisory.)"
        ),
    )
    mde: float | None = Field(
        default=None,
        description=(
            "Minimum detectable effect on weighted_score, from the replicate null split. None when "
            "it could not be computed; 0.0 is a real answer meaning the replicates agreed exactly."
        ),
    )
    integrity_checks: list[GuardrailCheck] = Field(
        default_factory=list,
        description=(
            "The two readings the method's promote-only-when list requires and a human used to do "
            "by eye: engagement recall.yes and completion rate, per arm. A GuardrailCheck list "
            "rather than four scalars because that is exactly what the type already models — one "
            "non-primary quantity that may veto a promotion — and it renders through the same "
            "helper as sibling_checks on the other track."
        ),
    )
    guardrails: list[GuardrailCheck] = Field(
        default_factory=list, description="Cost / latency guardrails. Advisory in the model, gating in the render."
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

    `metric` joins the key for a sharper reason than the rest: a floor is now measured on two
    different quantities. The activation track's is a floor on `f1.yes`, the execution track's on
    per-row `weighted_score`, and on the SAME suite, variant, model and row count they are
    different numbers. Without `metric` in the key one track's lookup is handed the other track's
    measurement — and a floor decides whether a round runs at all.
    """

    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1, description="Suite the floor was measured on (the pre-fan-out task_id).")
    variant_id: str = Field(min_length=1, description="Arm the null comparison split. Renamed per round, so keyed.")
    model: str = Field(
        min_length=1, description="Model the rows ran under. Sourced ONLY from optimize_gate.resolve_model."
    )
    metric: str = Field(
        default=f"f1.{TARGET_LABEL}",
        min_length=1,
        description=(
            "What the floor is a floor ON. 'f1.yes' for the activation track, 'weighted_score' for "
            "the execution track. Part of the cache key: the two are different numbers on the same "
            "suite, and a lookup that ignored this would be handed the other track's measurement."
        ),
    )
    criterion_index: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Criterion position the floor was measured on. None when the metric is not per-criterion "
            "— the execution track's weighted_score is the row's, not a criterion's."
        ),
    )
    n_rows: int = Field(ge=0, description="Rows scored in BOTH halves of the split — not the suite's row count.")
    n_invocations: int = Field(ge=0, description="Invocations the null comparison was split across.")
    n_replicates: int | None = Field(
        default=None,
        ge=2,
        description=(
            "Replicates per row the split used, after balancing — the execution track's split AXIS, "
            "and None on the activation track where `n_invocations` already is it. In the key "
            "because it moves the number and nothing else in the key would catch it: measured on "
            "one suite, 0.099 at `--repeats 3` against 0.169 at `--repeats 2`, with `n_invocations` "
            "equal to 1 in both. Without this field the cache serves one floor for the other, "
            "silently, on the number that decides whether a round runs at all."
        ),
    )
    confidence: float = Field(gt=0.0, lt=1.0, description="Interval width used. A wider interval is a wider floor.")
    seed: int = Field(description="Bootstrap seed. Two seeds give two (close, but different) floors.")
    n_resamples: int = Field(gt=0, description="Bootstrap draws. Fewer draws, coarser floor.")
    mde: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "The half-width: the smallest difference this suite can resolve. Read `metric` for WHICH "
            "difference — an f1.yes difference on the activation track, a weighted_score difference "
            "on the execution track. Bounded [0, 1] either way: weighted_score is itself bounded "
            "[0, 1], so a half-width on a difference of two per-row means is too."
        ),
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
    instance_best_front: list[str] = Field(
        default_factory=list,
        description=(
            "Variant ids achieving the highest score on at least one row (GEPA's frontier), which is "
            "a DIFFERENT set from pareto_front: ours is the right set for discarding, this one for "
            "merging, because it deliberately retains an arm that wins exactly one row."
        ),
    )
    lineage_head: str | None = Field(
        default=None,
        description=(
            "The arm the SEARCH LOOP carries into the next round, or None when the round accepted "
            "nothing. NOT a promotion: a search accept is an unpaired train win, and only Stage B "
            "plus Stage C advance the incumbent the skill diffs for the user. The score to beat is "
            "derived from this arm's row_scores above rather than stored again here."
        ),
    )

    @model_validator(mode="after")
    def _lineage_head_is_readable(self) -> RoundScores:
        """A named head must be an arm here, and an arm with scores to derive a number from.

        Both states are otherwise silent at write time and surface a round later, in the user's
        terminal, from a sidecar written a round earlier. The next round's search loop looks the
        head up in `arm_row_scores` and averages its `row_scores`, so an absent arm raises
        `StopIteration`, and an empty one reaches the snippet's no-shared-rows exit — which blames
        an unpinned `dataset.sample_seed` and sends the reader to the wrong problem entirely.
        """
        if self.lineage_head is None:
            return self
        head = next((a for a in self.arm_row_scores if a.variant_id == self.lineage_head), None)
        if head is None:
            raise ValueError(
                f"lineage_head {self.lineage_head!r} is not one of this round's arms "
                + f"{sorted(a.variant_id for a in self.arm_row_scores)}"
            )
        if not head.row_scores:
            raise ValueError(
                f"lineage_head {self.lineage_head!r} scored no rows, so the next round has no score "
                + "to beat — record the round without a head instead"
            )
        return self


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
        default_factory=list,
        description=(
            "Cache, replaced in place on the whole key — every NoiseFloor field except `mde` and "
            "`computed_at`, derived from the model rather than listed here."
        ),
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
