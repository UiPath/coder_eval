"""Records produced by the `/coder-eval:optimize-skill` promotion gate.

Plain data, produced by :mod:`coder_eval.optimize_gate` rather than parsed from user input —
no discriminated union, no criterion type, nothing registered. They exist so the gate's verdict
is a typed value the skill prints, instead of arithmetic an agent performs by hand.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    tolerance: float = Field(description="How much of a relative increase/drop is tolerated before the check fails.")
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
            "Upper bound of that interval. Reported, not consulted: the promotion decision reads the "
            "Holm-corrected p-value, which is the same test with the family correction applied."
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
