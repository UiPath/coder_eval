# The execution gate's refusal causes, and why their ORDER is the rule

Subject: `optimize/execution.py::execution_gate`, `_execution_diagnostics`, `_refuse_*`,
`_below_mde_findings`, `models/optimize.py::ExecutionGateVerdict.gate_refusal`.

## First cause wins, and program order IS the precedence

Every cause answers the same question — *is this a result?* — with the same consequence, so they
share one field, one headline and one prose token. They differ in REMEDY, and a later cause is
usually an earlier one's consequence: if there was no comparison to make, the rows are moot; if the
rows never loaded, whether their differences vary is moot. So the earliest cause is the one whose
remedy comes first, and routing every setter through one sink says that once instead of leaving
eleven `if gate_refusal is None` guards to be kept in agreement.

The concrete case: a mistyped variant id makes that arm load ZERO rows as a consequence. Refusing on
the consequence replaces a message naming the two ids the experiment actually carries with one that
can only say "a wrong variant id, a wrong suite id or a wrong run directory".

## Why an arm with no rows is a refusal rather than a note

This track's statistic comes from `experiment.json`, not from the row tree — so it computes
perfectly well over rows that are not on disk, while every guardrail and integrity check reads green
over nothing. A valid experiment file beside a mistyped path renders as PROMOTED with every check a
green `— -> —`.

## The below-MDE refusal is deliberately TWO-SIDED

`mde` is the half-width of a bootstrap interval on a NULL difference, so a difference under it is
indistinguishable from the suite's own run-to-run noise however small the p is. But under the null a
candidate's difference is ALSO small: `abs(mean_diff) < mde` is true for nearly every candidate that
simply does not work — measured, 40 of 40 true-null candidates. Refusing all of them would retire
NOT PROMOTED almost entirely and send the reader to buy replicates for a candidate whose problem is
that it is null.

So the refusal is conditioned on the interval EXCLUDING zero. An interval that contains zero is the
data agreeing the candidate is null: an ordinary negative result, and it stays one. What is left for
the refusal is the pathology — a confident claim, in either direction, about an effect the
instrument cannot see.

## Zero variance splits into two messages

At a constant difference of ZERO the arms behaved identically, which is a finding about the
candidate that no number of extra rows can change, and `paired_t_test` reports p = 1.0 there rather
than the 0.0 a non-zero constant shift gives. One message would state a p the block below it
contradicts. The same split, for the same reason, as `holm_promote`'s `p_floor >= 1.0` branch.

## The interval-tighter-than-floor case is a caveat, NOT a refusal

The paired *t*'s interval comes from the BETWEEN-ROW spread of the differences, which is tiny
whenever the arms differ by a similar amount on every row, while `mde` measures WITHIN-row noise the
*t* never sees. So a real, large, consistent win reports an absurd p. Refusing it would be worse
than the defect: measured, a genuine 8-row 0.30 win reports a half-width of 0.007, the same shape as
the 0.400-on-every-row case. What is wrong there is the reported PRECISION, not the decision.

## Every note is suppressed under a refusal

A refusal says the comparison decided nothing; a note beneath it is a second, contradictory claim on
a page a user pastes into a promotion ledger. The below-MDE note calls itself "an ordinary negative
result", and it was the one rung that fired regardless — reproduced through the real gate, a
zero-variance refusal printed it beneath `NOT A RESULT`. `promoted` was unaffected, so this was
prose only.

`refused_already` is OR-ed with the local cause because "nothing has refused yet" has to include
what the diagnostics themselves decided three lines up. TWO paths arrive already refused and neither
returns early: the stale-tree cause and the primary-index cause.

## The primary-index refusal must be recorded BEFORE the diagnostics run

That ordering is load-bearing, not tidy. Recorded after, it produced a `NOT A RESULT —
primary_criterion_index=7 selected no usable row` headline above notes reading "this is an ordinary
negative result and not a measurement problem" and "the paired interval is tighter than this suite's
own noise floor" — measured.

`require_valid_criterion_index` bounds only BELOW, deliberately, since rows may legitimately differ
in criteria count and an over-long index should skip a row rather than raise. That is the wrong
answer here: an over-long primary index makes `row_score` return `None` on every row, so the vector
is EMPTY and indistinguishable from a suite whose rows all errored on that criterion.

## Dead weight is a READING and can never gate

Measured rather than argued: a constant criterion scales the paired difference vector without
changing its shape, so it scales the mean AND the standard deviation by the same factor — the paired
*t* is identical to 1e-12 between the grader-only and blended scales while the mean difference scales
by 1/2.05. The bootstrap interval scales with the data, the MDE is measured on the same blended
scale, and the guardrails never touch the blend, so EVERY conjunct of `promoted` is invariant to it.
Wiring it into `integrity_checks` would force `promoted = False` on comparisons that are
statistically sound — strictly worse than the presentational problem it would be fixing. The one
case where dead weight genuinely invalidates a comparison is every criterion being constant, which
is already the zero-variance refusal.

## A stale tree FLIPS the answer rather than merely being reported

`run.json` is written per INVOCATION while the tree is APPEND-ONLY, so a re-used `--run-dir` leaves
an earlier call's rows — or, with a smaller `--repeats`, its replicates — on disk, and they are
pooled into the comparison and into the checks that gate it. Measured on an identical winning
candidate: four unrecorded incumbent replicates moved `completion_rate` from 1.0 to 0.667 and
`promoted` from True to False, with no refusal and no note. Contaminate the candidate arm instead
and the error runs the other way.
