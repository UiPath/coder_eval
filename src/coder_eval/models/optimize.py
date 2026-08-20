"""Records produced by the `/coder-eval:optimize-skill` promotion gate.

Plain data, produced by the `optimize_*` decision family rather than parsed from user input —
no discriminated union, no criterion type, nothing registered. They exist so the gate's verdict
is a typed value the skill prints, instead of arithmetic an agent performs by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


# The label whose F1 the activation gate reads. `skill_triggered` emits `yes` / `no`, and
# "did the skill engage when it should" is the `yes` class.
#
# It lives HERE rather than in the gate family because `NoiseFloor.metric`'s default interpolates
# it, and this module cannot import those — they import these models, and the reverse is a
# cycle. Same cycle-free-leaf role `models/judge_defaults.py` plays for `DEFAULT_JUDGE_MODEL`.
TARGET_LABEL = "yes"


class GuardrailCheck(BaseModel):
    """One non-primary quantity that may veto a promotion.

    Covers both the sibling-regression checks (a candidate must not win by annexing another
    skill's requests) and the cost / latency guardrails. An unevaluable check reports
    ``passed=True`` with a ``note`` and ``None`` values — a missing measurement must never read
    as a pass on the merits, so the note is what the report prints instead of a number.
    """

    model_config = ConfigDict(extra="forbid")

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


def _failed_names(*check_lists: Sequence[GuardrailCheck]) -> list[str]:
    """The names of every check that FAILED, across the lists given, in the order given.

    One function rather than a comprehension in each verdict's ``failed_vetoes`` property, because
    two copies would put the polarity — ``not check.passed`` — in two places. That is the same shape
    CE037 and CE040 police for duplicated arithmetic: two spellings agree today and diverge the
    moment either is touched, silently, because both remain plausible. The properties reduce to a
    field selection over this.

    ``GuardrailCheck.passed`` is a non-optional ``bool`` and an unevaluable check reports ``True``
    with a note, so there is no third state to handle: an unmeasurable check is simply absent from
    the result, which is what lets a caller treat a non-empty list as "measured breach".
    """
    return [check.name for checks in check_lists for check in checks if not check.passed]


class GateVerdictBase(BaseModel):
    """Everything both tracks' Stage B verdicts say, declared once.

    A BASE CLASS, not a track-discriminated union. The distinction matters and the union argument
    below still holds: each subclass declares only its OWN extras, so no field arrives as
    permanently-``None`` noise on the track it does not belong to, and a reader never has to know
    which half applies. What the base removes is the second declaration of each shared field — the
    two tracks spelled all 18 twice, in hand-maintained parity that had already drifted in 14 of
    them — and with it the second expression of ``separated``, ``failed_vetoes`` and their order.

    The four statistic fields (``mean_diff``, ``ci_low``, ``ci_high``, ``p_value``) are declared
    **required** here, matching the activation track, and :class:`ExecutionGateVerdict`
    re-declares them with ``default=None``. A subclass re-declaration is the language-level way to
    say "same field, different default on this track": it keeps the difference in the class that
    has it. Which fields either subclass may re-declare, and why, is recorded ONCE — in
    :data:`_FIELD_OVERRIDES` at the foot of this module. Do not restate the set here or anywhere
    else; a second copy of a licence list is a licence that outlives its trade.

    **What is stable about ``model_dump()`` order, exactly:** an overridden field keeps its BASE
    position, so re-declaring one does not move it. Subclass-declared fields DO sit after every
    base field now, where several of them used to be interleaved — so both models' dump order
    changed when this base landed. Nothing reads a verdict positionally (the pins in
    ``tests/_fixtures/optimize_verdicts/`` are compared as PARSED JSON, ``reports_optimize`` reads
    by name, and no verdict is persisted), which is why those pins were deliberately NOT
    re-serialized: touching them would owe an estimator-ledger row for a change that moved no
    number.

    Statistics are ``None`` rather than fabricated when the sample cannot support them (mirroring
    ``PairedComparison``), and ``rows_excluded`` is first-class so a silently narrowed sample is
    visible.
    """

    model_config = ConfigDict(extra="forbid")

    incumbent_variant: str = Field(description="Variant id of the incumbent arm.")
    candidate_variant: str = Field(description="Variant id of the candidate arm under test.")
    suite_id: str = Field(description="Suite id (the pre-fan-out task_id) both arms ran.")
    # Required, not defaulted: the gate always knows both, and a default here would be a second
    # declaration of values the gate already owns — which is how a report ends up labelling a 90%
    # interval as 95%.
    confidence: float = Field(
        gt=0.0, lt=1.0, description="Interval width the estimator used, so the report cannot mislabel it."
    )
    n_resamples: int = Field(
        gt=0,
        description=(
            "Bootstrap draws. The estimator's own floor is 2/(n+1) — see reports_stats."
            "bootstrap_p_floor — so a p AT that floor is a resolution statement, not a measurement. "
            "A suite may have its own coarser floor above it, and where it does that one decides."
        ),
    )
    rows_paired: int = Field(
        description=(
            "Rows scored on BOTH arms — the paired sample the statistic was computed on, carried "
            "through from the pairing rather than recomputed here."
        )
    )
    rows_excluded: int = Field(
        description=(
            "Rows seen in at least one arm but left out of the pairing: present on one side only, or "
            "present on both and scored on only one (an errored or timed-out row produces no "
            "criterion result). Carried through from the pairing rather than recomputed here."
        )
    )
    mean_diff: float | None = Field(
        description="The paired point estimate of the difference, ALWAYS candidate - incumbent."
    )
    ci_low: float | None = Field(description="Lower bound of the interval on that difference.")
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
    gate_refusal: str | None = Field(
        default=None,
        description=(
            "Why this block is not a decision, or None when it is one. Renders as its own headline: "
            "a refusal is not a negative result, and reporting it as one is the defect this field "
            "fixes. The causes and their setters are per-track — see each subclass — but they all "
            "answer the same question with the same consequence, and each track's Holm pass forces "
            "`promoted=False` whenever it is set."
        ),
    )
    holm_alpha: float | None = Field(
        default=None, description="The family-wise alpha the track's Holm pass applied. None until it has run."
    )
    holm_rejected: bool | None = Field(
        default=None,
        description=(
            "Whether the Holm step-down rejected THIS verdict's null at its rank in the family. "
            "None until the track's Holm pass has run. Deliberately NOT derivable from the fields "
            "beside it: `holm_alpha` records the family-wide alpha and never the rank-dependent "
            "threshold, so a reader holding `p_value` and `holm_alpha` cannot tell a rejection from "
            "a near miss — the family SIZE decides, and only the function that saw the whole family "
            "knows it. Recorded because `promoted` alone conflates three different negatives (lost, "
            "underpowered, vetoed) and the rendered block has to tell them apart. "
            "See .claude/decisions/2026-08-20-the-promotion-decision.md."
        ),
    )
    promoted: bool | None = Field(
        default=None,
        description=(
            "None means gated but undecided — the track's Holm pass has not been applied. "
            "Otherwise it is the WHOLE decision: `holm_rejected` AND `separated` (the difference "
            "favours the candidate and the interval excludes zero) AND `gate_refusal` unset AND "
            "every veto list in `failed_vetoes` empty. A failed check FORCES False, so this field "
            "alone is safe to ship on. What it cannot tell you is WHY: lost, underpowered and "
            "vetoed all read False, and `holm_rejected` / `separated` are what tell those apart. "
            "The two tracks mean the SAME thing by it; what differs is only which lists each one "
            "HAS to check."
        ),
    )
    mde: float | None = Field(
        default=None,
        description=(
            "Minimum detectable effect for this suite at this size, in the track's own metric, from "
            "its null split. None when it could not be computed; 0.0 is a real answer meaning the "
            "null split's arms agreed exactly."
        ),
    )
    guardrails: list[GuardrailCheck] = Field(
        default_factory=list,
        description=(
            "Cost / latency guardrails. A failure forces `promoted = False` in the track's Holm "
            "pass and is named in the rendered block's BLOCKED headline."
        ),
    )
    notes: list[str] = Field(
        default_factory=list, description="Everything the reader needs to distrust or qualify the numbers above."
    )

    @property
    def _own_vetoes(self) -> list[GuardrailCheck]:
        """The track's OWN veto list — `sibling_checks` on activation, `integrity_checks` on execution.

        A plain property raising rather than an ``abc.abstractmethod``: pydantic's metaclass and
        ``ABCMeta`` do not compose, and the two subclasses here are the only implementors the tree
        will ever have.
        """
        raise NotImplementedError(
            "GateVerdictBase is not constructed directly — ActivationGateVerdict and "
            + "ExecutionGateVerdict each name their own veto list"
        )

    @property
    def separated(self) -> bool:
        """True when the paired comparison itself separated, guardrails aside.

        The ONE declaration of "the statistic came out in the candidate's favour and its interval
        excludes zero", for both tracks. Each track's Holm pass needs it to decide ``promoted``;
        ``reports_optimize`` needs it to tell a candidate that LOST from one that WON AND WAS
        BLOCKED. The renderer read ``promoted`` for that second question until the guardrail was
        folded in, at which point the BLOCKED rung became unsatisfiable and a blocked candidate
        degraded silently to the ordinary ``NOT PROMOTED`` headline — the one thing a reader must
        not confuse it with.

        Note what it deliberately does NOT include: the Holm rejection. Holm is a property of the
        FAMILY and this is a property of one verdict, so folding ``i in rejected_at`` in here would
        put a family decision on a model that cannot see the family. :attr:`holm_rejected` carries
        that half, stored, because it cannot be derived here.

        A property rather than a stored field on purpose: nothing new is serialized, so no
        construction site can set it inconsistently with the numbers it derives from — the same
        reason ``SearchComparison.accepted`` is a property over ``beats`` / ``blocker``.
        """
        return self.mean_diff is not None and self.mean_diff > 0.0 and self.ci_low is not None and self.ci_low > 0.0

    @property
    def failed_vetoes(self) -> list[str]:
        """Every check that vetoed this candidate — the track's own list first, then cost/latency.

        BOTH veto lists, and that is the whole point. A candidate on this list **won its comparison
        and was vetoed**, which is a different outcome from losing and calls for the opposite next
        action: a loss says try a different idea, a veto says this idea works and costs too much or
        broke something else. Reading ``guardrails`` alone is not a narrower version of that
        question, it is a wrong answer to it — a failing sibling check rendered as the ordinary
        ``NOT PROMOTED`` headline until that was found.

        The ONE declaration of the set and of its ORDER, so ``promoted`` and the rendered
        ``BLOCKED BY A GUARDRAIL`` rung cannot disagree about what vetoes. Order is preserved
        because the renderer joins it into a sentence; a name appearing in both lists appears twice.

        A property rather than a ``computed_field``, for :attr:`separated`'s reason and one more: a
        computed field would enter ``model_dump()``, and every pinned ``optimize_verdicts/*.json``
        would gain a key for a value that measures nothing new.
        """
        return _failed_names(self._own_vetoes, self.guardrails)


class ActivationGateVerdict(GateVerdictBase):
    """The activation track's Stage B verdict for ONE candidate against the incumbent.

    Declares only its own seven fields; the other 18 come from :class:`GateVerdictBase` and it
    overrides none of them.

    ``promoted`` is deliberately ``bool | None``: a single gate cannot decide a family, so
    :func:`coder_eval.optimize.activation.activation_gate` leaves it ``None`` and only
    :func:`coder_eval.optimize.activation.holm_promote` — which sees every survivor at once — sets it.
    Rendering a ``None`` as a non-promotion would let a forgotten Holm pass look like an honest
    negative result.

    ``gate_refusal`` has TWO setters on this track, told apart by ``p_value`` rather than by a
    second field. (1) ``holm_promote`` sets it when the suite's discreteness floor exceeds this
    candidate's Holm threshold — the gate structurally cannot separate at this size; that one
    always carries a p, because it is only reachable inside the ``p_value is not None`` branch, and
    renders as CANNOT SEPARATE AT THIS SIZE. (2) ``activation_gate``'s row-selection preflight sets
    it when the two arms recorded different ``--split`` values — they never scored the same rows,
    so no comparison was made; that one always has ``p_value is None`` and renders as NOT A RESULT.
    """

    criterion_index: int = Field(
        ge=0, description="Position of the gated criterion in the suite's success_criteria list (0-based)."
    )
    incumbent_f1: float | None = Field(description="Incumbent's f1.yes pooled over the paired rows.")
    candidate_f1: float | None = Field(description="Candidate's f1.yes pooled over the paired rows.")
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
            "optimize.activation.min_discordant_rows, which computes it). What does NOT help is adding "
            "rows the arms agree on — at a fixed R that RAISES the floor."
        ),
    )
    range_non_overlap: bool = Field(
        default=False,
        description=(
            "DIAGNOSTIC ONLY: min(candidate per-invocation F1) > max(incumbent's). The former gate, retained "
            "as a reported observation and never consulted in the promotion decision."
        ),
    )
    sibling_checks: list[GuardrailCheck] = Field(
        default_factory=list, description="Per-sibling recall.yes regression checks. A failure blocks promotion."
    )

    @property
    def _own_vetoes(self) -> list[GuardrailCheck]:
        return self.sibling_checks


class ExecutionGateVerdict(GateVerdictBase):
    """The execution track's Stage B verdict for ONE candidate against the incumbent.

    Declares its own five fields, plus the re-declarations :data:`_FIELD_OVERRIDES` licenses —
    every one of them is on this class. The statistic fields among them carry ``default=None``
    because this track's gate can return before any statistic exists, while the activation gate
    always computes them or refuses; the rest are re-declared for a description this track needs to
    say differently. The set itself is recorded only in ``_FIELD_OVERRIDES``, never restated here.

    Sharing a base with :class:`ActivationGateVerdict` is deliberately NOT the same as being one
    track-discriminated union with it. A union would carry ``p_floor`` / ``n_discordant`` /
    ``criterion_index`` as permanently-``None`` noise on one side and ``effect_size`` on the other,
    and every reader would have to know which half applied; a base class carries only what both
    tracks really have, and each subclass declares its own extras.

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

    n_resamples: int = Field(
        gt=0,
        description=(
            "Bootstrap draws used for the MDE and the cost/latency guardrails this verdict carries "
            "— NOT for the primary statistic, which is an analytic paired t. Recorded for the same "
            "reason ActivationGateVerdict records it: without it the same block can be produced at "
            "two very different resolutions and read identically."
        ),
    )
    mean_diff: float | None = Field(
        default=None,
        description="Paired mean difference in per-row weighted_score, ALWAYS candidate - incumbent.",
    )
    ci_low: float | None = Field(default=None, description="Lower bound of the paired-t interval on that difference.")
    ci_high: float | None = Field(default=None, description="Upper bound of that interval.")
    p_value: float | None = Field(default=None, description="Paired t-test p. None when fewer than 2 rows paired.")
    gate_refusal: str | None = Field(
        default=None,
        description=(
            "Why this block is NOT a decision, or None when it is one. Set by execution_gate, "
            "MOST-SPECIFIC-FIRST, for any of seven causes: no comparison to make, a stale tree, an "
            "experiment that resolves nothing, an arm that loaded ZERO rows, too few rows paired, "
            "zero-variance paired differences, or a difference below the suite's own MDE whose "
            "interval STILL excludes zero. One field for all of them because they answer the same "
            "question with the same consequence; the message names which cause and its remedy. It "
            "forces promoted=False but does NOT drop the verdict from the Holm family. Note the "
            "DIFFERENT setters from ActivationGateVerdict.gate_refusal. "
            "See .claude/decisions/2026-08-20-the-execution-gate-refusals.md."
        ),
    )
    effect_size: float | None = Field(
        default=None,
        description=(
            "Cohen's d for the paired difference. None does NOT mean the comparison failed — d is "
            "undefined at zero variance, which two arms agreeing exactly on every row produce."
        ),
    )
    primary_criterion_index: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The PREDECLARED primary criterion, by position in the suite's success_criteria. When "
            "set, `primary_mean_diff` reports the paired difference on that criterion alone, so the "
            "effect is readable in the grader's own unit next to the blended one. **The VALUE it "
            "reports never affects `promoted`** — but an index selecting NO usable row while the "
            "blended statistic had rows IS a `gate_refusal`, which does force promoted=False. "
            "Distinct from `engagement_criterion_index` (an integrity CHECK's subject) and from "
            "ActivationGateVerdict.criterion_index (that gate's metric SOURCE). "
            "See .claude/decisions/2026-08-20-the-execution-gate-refusals.md."
        ),
    )
    primary_mean_diff: float | None = Field(
        default=None,
        description=(
            "The paired mean difference on `primary_criterion_index` alone, ALWAYS "
            "candidate - incumbent like `mean_diff` beside it. `None` when no primary was "
            "predeclared, or when that criterion produced no usable paired row. A READING: it is "
            "what lets a reader convert the blended `mean_diff` back into the unit the grader "
            "actually scores in, and it gates nothing."
        ),
    )
    dead_weight: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "The share of total criterion weight held by criteria whose PAIRED DIFFERENCE is "
            "identically zero on every row they scored. `weighted_score` is a weighted mean over "
            "every criterion, so such a criterion contributes its whole weight to that mean's "
            "denominator and nothing to its difference — reported so a reader can convert "
            "`mean_diff` back into the grader's own unit. `None` means it could not be computed "
            "(most often a run predating `CriterionResult.weight`) and is deliberately never 0.0, "
            "which would claim no dilution; the reason is always in `notes`. **It is a READING and "
            "can never gate** — a permanent architectural decision, measured rather than argued: see "
            ".claude/decisions/2026-08-20-the-execution-gate-refusals.md."
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

    @property
    def _own_vetoes(self) -> list[GuardrailCheck]:
        return self.integrity_checks


# The fields :class:`ExecutionGateVerdict` re-declares from :class:`GateVerdictBase`, and why.
#
# Read by `tests/test_optimize_layering.py`, which asserts BOTH directions: no subclass may
# re-declare a base field absent from here, and every entry here must genuinely differ from the
# base — in default, in description, or both. That second half is the CE038 `EXEMPT` pattern: a
# stale licence must not outlive the trade it recorded.
_FIELD_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("mean_diff", "optional here: the execution gate can return before any statistic exists"),
    ("ci_low", "optional here, with the paired-t interval named"),
    ("ci_high", "optional here"),
    ("p_value", "optional here, and an analytic paired t rather than a bootstrap draw"),
    ("n_resamples", "the draws pay for the MDE and the guardrails, NOT for the primary statistic"),
    ("gate_refusal", "four kinds of cause, all detected inside execution_gate rather than at Holm"),
)


class ConfirmVerdict(BaseModel):
    """Stage C's computed verdict: did the Stage B effect REPRODUCE on the held-out split?

    A DECISION RECORD, so a `BaseModel` with ``extra="forbid"`` beside
    :class:`ActivationGateVerdict` and :class:`ExecutionGateVerdict`, and for their reason: a
    mistyped field on a model whose job is to say what a promotion rests on must raise rather than
    land at a default (CE041's rationale). That is the opposite filing decision from a computed
    READING like ``SeedStability`` or ``RuleCeiling``, which are NamedTuples beside the functions
    that produce them because they are rendered and never persisted.

    **Stage C is a family of ONE, and that is correct rather than an oversight.** Only the Stage B
    winner is confirmed, so there is no multiplicity to correct — a reader who expects Holm here is
    looking for a correction over hypotheses that were never tested.

    **The train effect is READ, never recomputed.** It comes off the Stage B verdict, so the two
    numbers this block compares cannot disagree with the blocks they were reported in.
    """

    model_config = ConfigDict(extra="forbid")

    incumbent_variant: str = Field(description="Variant id of the incumbent arm on the confirm run.")
    candidate_variant: str = Field(description="Variant id of the ONE candidate being confirmed.")
    suite_id: str = Field(description="Suite id (the pre-fan-out task_id) both arms ran.")
    train_effect: float | None = Field(
        default=None,
        description=(
            "candidate - incumbent on the TRAIN split, read off the Stage B verdict rather than "
            "recomputed. `None` when that verdict measured none."
        ),
    )
    test_effect: float | None = Field(
        default=None,
        description=(
            "The same quantity on the confirm run. `None` means no comparison was made there — not "
            "an effect of zero, which is a measurement."
        ),
    )
    test_mde: float | None = Field(
        default=None,
        description=(
            "The confirm split's OWN minimum detectable effect, read off the confirm gate's verdict — "
            "so nothing here re-measures it. `execution_gate` measures that floor on every path; "
            "`activation_gate` does NOT, because its row-selection preflight returns before the "
            "measurement, which is one more way this arrives `None`. `None` "
            "or 0.0 leaves the SHRANK/REPRODUCED margin UNDEFINED, and the outcome is then "
            "`undecided` rather than silently SHRANK: a floor of 0.000 means the floor could not be "
            "priced, never that this suite can resolve anything."
        ),
    )
    delta: float | None = Field(
        default=None,
        description="test_effect - train_effect. `None` when either side is None.",
    )
    outcome: Literal["reproduced", "shrank", "reversed", "undecided"] = Field(
        description=(
            "The train->test classification, from `optimize.gate.classify_confirm`. `reversed`: the "
            "test effect's sign opposes the train effect's. `shrank`: same sign, but below the train "
            "effect by more than the confirm split's MDE — and a test effect of exactly 0.0 is "
            "SHRANK, not reproduced, because 'same sign' is undefined there. `reproduced`: same "
            "sign, within that margin. `undecided` has FIVE causes: the confirm gate refused; either "
            "effect is absent; the TRAIN effect is not a win; the margin is undefined (`None`, or "
            "anything below `optimize.gate.FLOOR_RESOLUTION` rather than only exactly 0.0); or a "
            "non-finite value reached the comparison. "
            "See .claude/decisions/2026-08-20-stage-c-confirmation.md."
        )
    )
    test_verdict: ActivationGateVerdict | ExecutionGateVerdict = Field(
        description=(
            "The FULL confirm-gate block, carried rather than summarized: every check, guardrail and "
            "note the confirm run produced is what a reader needs to decide whether to trust the "
            "classification above it."
        )
    )
    confirm_refusal: str | None = Field(
        default=None,
        description=(
            "Why this is NOT a comparison, or None when it is one — mirroring `gate_refusal` on both "
            "gate verdicts, including that it forces `outcome = undecided`. Set when the confirm run "
            "recorded any `--split` other than `test`, when the confirm gate itself refused, or when "
            "the train verdict refused. Naming more than one candidate does NOT land here: that "
            "raises `TypeError` at the call, because a shortlist is a caller error rather than a "
            "property of the measurement. A train verdict that merely did not PROMOTE is a NOTE "
            "rather than a refusal. "
            "See .claude/decisions/2026-08-20-stage-c-confirmation.md."
        ),
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

    `split` joins the key because it selects a fixed, NAMED row set — unlike the two samplers,
    which are deliberately out of it (see the field). On the shipped `outcome.yaml` template it is
    the only field above `mde` that differs between the train and the test measurement, so without
    it a train floor is served to a test lookup on a suite where every other key field is equal.

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
        min_length=1, description="Model the rows ran under. Sourced ONLY from optimize.execution.resolve_model."
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
    split: str | None = Field(
        default=None,
        description=(
            "The ``--split`` the runs this floor was measured over recorded, derived from their "
            "`run.json`. Part of the cache KEY, so a train floor is never served to a test lookup. "
            "`None` means no `--split` was passed (a full-suite run), which is deliberately not the "
            "same as the sentinel a dir with unreadable provenance collapses to. "
            "See .claude/decisions/2026-08-20-the-noise-floor.md."
        ),
    )
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
    grader_fingerprint: str | None = Field(
        default=None,
        description=(
            "A hash of the outcome grader AND its expectations at the time this round ran, from "
            "`verify.py --fingerprint`. Scores are only comparable across rounds that share it: a "
            "mid-round grader fix moved a suite mean 0.8679 -> 0.9158 on IDENTICAL artifacts, and "
            "nothing in any run directory recorded that the instrument had moved. `None` means NOT "
            "RECORDED — a round written before this field, or one whose suite has no script grader "
            "— which is deliberately not the same as a recorded fingerprint that happens to differ, "
            "and is why `grader_changed` answers None rather than True for it. Reported, never "
            "enforced: a changed instrument makes two rounds incomparable, which is a fact about "
            "the measurement, not a veto on a promotion."
        ),
    )
    suite_fingerprint: str | None = Field(
        default=None,
        description=(
            "A hash of the SUITE this round scored — every criterion's concrete parameters, the "
            "prompt template as authored, the EXPANDED rows (id, prompt and substituted criteria) "
            "and the whole `run_limits` block. `None` on a round that recorded none, which "
            "`optimize.store.suite_changed` reports as 'cannot tell' rather than as a match. "
            "Complements `grader_fingerprint` beside it: that one covers the outcome track's script "
            "and answer key and cannot see a `weight` change that re-blends `weighted_score`, and "
            "the activation track has no script grader at all. "
            "See .claude/decisions/2026-08-20-instrument-provenance.md."
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
