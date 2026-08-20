# The rendered verdict block: five rungs, and the order is the contract

Subject: `reports_optimize.py::_headline`, `render_markdown`, `render_execution_markdown`,
`render_confirm_markdown`.

## It replaced two hand-written chains that drifted twice

The activation `BLOCKED` rung once read `verdict.guardrails` alone while its twin unioned both veto
lists, so a candidate that separated, cleared Holm and was vetoed by a failing SIBLING check
rendered `NOT PROMOTED` — indistinguishable from one that simply lost. Before that it keyed on
`promoted`, which the guardrail veto had made unsatisfiable.

## The three conjuncts of the BLOCKED rung, each load-bearing

- **NEVER `promoted`.** Both Holm passes fold the veto into it, so a blocked candidate arrives with
  `promoted is False` and keying on that field makes the rung unreachable — dropping a blocked
  winner into `NOT PROMOTED`, the one rung it must never be confused with.
- **`holm_rejected`**, because `separated` alone is the trap on the other side: `separated` is a
  property of ONE verdict and deliberately excludes the family decision, so at `m > 1` a p between
  `alpha/m` and `alpha` leaves `ci_low > 0` while Holm rejects nothing. Measured: two candidates at
  p = 0.03 in a family of two, identical in every statistic, rendered BLOCKED and NOT PROMOTED
  purely because one carried a failing cost check — with the note ladder printing the contradicting
  "did not clear the Holm threshold" line directly underneath.
- **`failed_vetoes` rather than `guardrails`**, which spans both of a track's veto lists.

## Why one chain taking three arguments rather than a rung table

The two tracks differ by exactly three strings. A table plus an evaluator would add indirection a
reader has to unwind to answer "what does this print?", for two call sites in one file. The
execution track passes `NOT A RESULT` as its `refusal_label`, which is why its ladder reads as four
rungs rather than five — it reaches rung 3 with the same text rung 2 produces. That is a property of
the argument, not a special case in the chain.

## `render_confirm_markdown` is deliberately NOT folded in

Its `REVERSED` rung is Stage-C-specific, there is exactly one confirm renderer, and generalizing for
one caller is the speculation YAGNI forbids.

## Why the two body renderers were not merged

`_headline` is shared and the remaining bodies are two genuinely different field lists. Merging them
would need a field-order table — indirection for two call sites in one file.

## The presentation layer reads no disk and decides nothing

Pinned by a test, and that boundary is what makes the split real. Its one runtime statistics import
is `reports_stats.bootstrap_p_floor`, which the same test REQUIRES: it makes the p-floor value
derived rather than respelled (CE040).
