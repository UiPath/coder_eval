# Stage C: did the Stage B effect reproduce?

Subject: `optimize/gate.py::classify_confirm`, `build_confirm_verdict`, `confirm_split_check`,
`confirm_one_candidate`, `optimize/activation.py::confirm_gate`,
`optimize/execution.py::confirm_gate_execution`, `optimize/activation.py::gate_seed_stability`.

## A family of ONE, and that is correct

Only the Stage B winner is confirmed, so there is no multiplicity to correct. A reader who expects
Holm here is looking for a correction over hypotheses that were never tested. Holm is still applied
at `m = 1`, purely so the carried block is a DECIDED one rather than rendering as `UNDECIDED`.

## The train effect is READ, never recomputed

It comes off the Stage B verdict, so the two numbers the block compares cannot disagree with the
blocks they were reported in.

## The margin is the confirm split's OWN MDE

Which is what makes the rule per-track without a second declaration: each track passes its own
gate's floor, on its own metric. Picking a different multiple on one track would be a second
declaration of "how much shrinkage is real". A floor of `None` or 0.0 leaves the margin UNDEFINED
and the outcome is `undecided` rather than silently SHRANK — 0.000 means the floor could not be
priced, never that the suite can resolve anything.

## Why the split check is shared and must not be an if/elif over the collapsed value

`SplitProvenance.value` collapses to `UNRECORDED_SPLIT` when ANY pooled dir is unreadable. So a chain
reading `if unrecorded: note / elif value != "test": refuse` drops the refusal entirely for three
dirs recording `train` beside one unreadable `run.json` — and the confirm then classifies over TRAIN
rows carrying only a "provenance is missing from 1 of 4" note. That is precisely the failure the
refusal exists for.

The execution twin takes ONE run dir and cannot reach that state, which is exactly why the rule may
not live on each track separately: the safe one would keep working while the other drifted. The
activation side had already gained a "not the Stage B winner" note the execution side lacked, while
its docstring claimed both worked "for the reasons the execution twin's docstring gives".

## A recorded `train` is a REFUSAL; an unrecorded split is a NOTE

A recorded `train` means Stage C silently re-ran the train rows, at full price, with no error
anywhere — an effect reproduces on its own training data by construction. An unrecorded split is a
run predating the field.

## Seed stability carries no single `promoted` field

Collapsing three disagreeing seeds into one verdict is the exact thing it exists to prevent: a
decision that flips with the seed is a coin flip, and reporting the majority's answer as *the* answer
hides that. Its `promote_agreement` counts promotions at a family of ONE, which is not the round's
decision when the round gated more than one candidate — stated because the number invites that
reading.
