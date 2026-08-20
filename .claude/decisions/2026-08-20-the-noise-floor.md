# The noise floor: what it measures, and the four ways it read zero

Subject: `optimize/gate.py::floor_preflight`, `no_floor`, `floor_from_clusters`,
`optimize/activation.py::measure_noise_floor`, `noise_floor_mde`,
`optimize/execution.py::measure_execution_noise_floor`.

## A silent `None` is indistinguishable from a floor of zero

Both floor functions return `None` for several distinct reasons, and the caller is an agent about to
decide whether to spend money. Verified against the shipped code: `noise_floor_mde` with a mistyped
run directory returned a bare `None` and printed nothing — on the one function whose job is to stop
a user spending. `no_floor` is now the single reporting channel, and an unconfigured
`logging.warning` reaches stderr through Python's last-resort handler, so the agent driving the
skill's inline snippet sees it without any logging setup.

`reasons` is an out-parameter SINK rather than a widened return type because `noise_floor_mde` is
public and imported by those snippets: changing its `float | None` would break a user's terminal.

## Why the preflight's order cannot be reversed

Reconcile BEFORE load, so a contaminated tree costs no parse — and, more importantly, so a WRONG
path still wins its own case: a wrong path leaves nothing on disk to be unrecorded. Reversed, a
mistyped variant id reports a contaminated tree and sends the reader to check `--repeats` instead of
the path they mistyped.

Measured on dirs `activation_gate` correctly refuses: `measure_noise_floor` returned a floor
computed over an extra pooled row, and `arm_row_scores` returned the stale row in its vector. The
floor decides whether a round runs at all.

## An unrecorded dir is a NOTE, never a refusal

The family's settled missing-provenance stance, so old run dirs stay measurable. `reconcile_arms`
logs it; neither floor has a `notes` channel to surface it in, which is why the count is unused
there.

## A floor of exactly 0.000 is a real answer

It means every row's replicates agreed exactly — a deterministic suite, or one whose rows all failed
the same way. `measured.mde if ... is not None`, never `measured.mde or None`: truthiness would
erase it. A reader who is not told this reads "Minimum detectable effect: 0.000" as "this suite can
resolve anything", which is the opposite of what an unmeasurable floor means.

## Balancing before splitting, on both tracks

`cluster_bootstrap_diff_ci` pools the drawn clusters' OBSERVATIONS before applying the statistic, so
an unbalanced row weighs 2:1 across the halves while a balanced one weighs 1:1 — and between-row
spread then leaks into a difference that is supposed to be zero by construction. Measured: 8 rows
with NO within-row variance report 0.000 at uniform counts and 0.056 when half of them carry 2
replicates.

## The execution floor measures `weighted_score`, not `f1.yes`

Computing an F1 floor for a gate that never reads F1 is the bug that function replaced. On the
bundled outcome template it returned a confidently meaningless 0.000.

## An odd count splits unevenly, and that is the safe direction

Three replicates split 2/1, which widens the interval and therefore reports a CONSERVATIVE floor —
the same on both tracks.
