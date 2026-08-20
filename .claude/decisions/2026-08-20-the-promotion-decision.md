# What `promoted` means, and every way it has been wrong

Subject: `optimize/gate.py::decide_family`, `optimize/activation.py::holm_promote`,
`optimize/execution.py::holm_promote_execution`, `models/optimize.py::GateVerdictBase.promoted`.

## The field used to mean two different things

`promoted` was computed in two places, 700 lines apart, and the two expressions were not the same
one. The activation track folded in its `sibling_checks` and left the cost/latency `guardrails`
advisory — for the skill's PROSE to gate on — so a candidate that materially raised what a row cost
came back `promoted=True` while the rendered block called it BLOCKED. A caller reading the field
could ship what the page said not to.

Both tracks now go through one loop and one conjunction: Holm rejected AND `separated` AND no
refusal AND `failed_vetoes` empty. `failed_vetoes` is the single declaration of which lists veto.

## Folding the veto in is only safe because the statistical half has its own name

`separated` exists so the renderer can tell a candidate that LOST from one that WON AND WAS BLOCKED.
Read `promoted` for that second question and the BLOCKED rung becomes unsatisfiable the moment the
veto is folded in — a blocked winner degrades silently to the ordinary NOT PROMOTED headline, which
is the one thing a reader must not confuse it with, because the two call for opposite next actions.

Measured: a candidate whose sibling check failed rendered as NOT PROMOTED, indistinguishable from
one that simply lost, until `failed_vetoes` was made the single declaration (`cab79de`).

## `holm_rejected` is stored because it cannot be derived

`holm_alpha` records the family-wide alpha, never the rank-dependent threshold, so a reader holding
`p_value` and `holm_alpha` cannot tell a rejection from a near miss — the family SIZE decides, and
only the function that saw the whole family knows it. Without the field, the BLOCKED headline also
fires on a candidate the family correction never rejected, sending the reader to fix cost when the
real problem is power. Measured: two candidates at p = 0.03 in a family of two, identical in every
statistic, rendered BLOCKED and NOT PROMOTED purely because one carried a failing cost check.

## The refusal conjunct is load-bearing on both tracks

Not belt-and-braces. On activation, `p_floor` bounds the p's EXPECTATION, so a realized p dips below
it on roughly half of all seeds — measured, 16 of 30 on the 6-row fixture at 20,000 draws. On
execution, a zero-variance verdict reports p = 0.0000 over a zero-width interval, so `separated`
holds on it too. Without the conjunct an undecidable comparison promotes AND carries a refusal: two
contradictory claims in one block, which is the defect `gate_refusal` exists to fix, reborn.

## A refused verdict with a real p stays in the family

Membership is `p_value is not None` and nothing else. Holm corrects for the hypotheses actually
tested, and dropping a measured-but-degenerate candidate shrinks `m` and LOOSENS `alpha/m` for its
siblings — the uncorrected-`p <= alpha` degeneration from the other side. Measured: three gate runs
with two below-MDE refusals promoted a p = 0.027 sibling that a family of three rejects.

## Why the family size is `len(family)` and not `len(verdicts)`

The two differ exactly when a member has no p. Mutating it to `len(verdicts)` passed the ENTIRE
suite, because every case in the sensor class used a family whose members were all measured. Three
assertions now cover it — the ladder rung, the trailing note and the activation refusal's own
sentence — and all three need a mixed-membership family to say anything.

## One measurable difference from the two loops it replaced

The execution wrapper omitted `gate_refusal` from its `copy_with`; the unified loop writes it on
every measured path, from the hook, which returns the verdict's own value. The VALUE is unchanged —
verified over a 10,982-state differential against both old loops — but the key now enters
`__pydantic_fields_set__`, so `model_dump(exclude_unset=True)` includes it on an execution verdict
built without it. Nothing reads a gate verdict that way today.
