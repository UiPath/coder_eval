# The skill-facing API: why `optimize/api.py` exists, and what it is not

`/coder-eval:optimize-skill`'s `SKILL.md` carried **427 lines of python in fifteen fences**. They were
not import lines. They were guards (`if not arms[0].row_scores: raise SystemExit(…)`), fallbacks (a
suite-level ceiling when rule attribution is unavailable — the prose called it "the difference between
an answer and a missing row"), track branches (a commented-out half plus a `TRACK = "activation"`
string the user hand-edited), session continuity (Step 11's own prose warned "run it in a fresh
interpreter and it fails with `NameError`… after the round has already been paid for"), and one
hand-written row primitive reaching into `r.success_criteria_results[grader_index].score` with no
bounds check.

Markdown does not execute, so none of it was reachable by a test. What that cost, measured rather
than argued: **two fences computed a reported number from a run tree they never reconciled** — the
ceilings table and the per-row replicates. CE053 exists to force exactly that reconcile and could not
see them, because the rule reads `.py` files. `grep -c reconcile SKILL.md` was `0`.

`optimize/api.py` is rank 4 of the family: 18 composites over those fifteen fences, each returning the
markdown block the skill prints. Fence lines went **427 → 126**.

## Why composites and not a facade

A module re-exporting the 46 primitives would have left every guard, fallback and branch in markdown.
The point was never to shorten the import lines; it was to put the *logic* somewhere a test can reach
it. `api.py` therefore exports composites **only**, and a fence that still needs a primitive is a
fence not finished — which **CE066** makes mechanically visible.

This is also why `optimize/__init__.py`'s no-facade rule needed no amendment: its two sensors filter
by `__module__`, so a module's own composites satisfy "defines nothing" while its `from .load import x`
names are excluded.

## Why every composite returns `str`

The renderers already state each decision **in words inside the block** —
`reports_optimize.py` renders `"ACCEPT into the lineage"` / `"REVERT — the head stands"`, and
`"DO NOT ACCEPT"` / `"CANNOT COMPARE"`. A caller prints the string into its ledger, and a reader of
that ledger sees the same sentences the caller acted on. A wrapper model would add a second
representation of a decision that is already unambiguous (YAGNI), and each composite's regression test
is then a whole-string comparison against a direct library call.

## Why the new blocks live in `reports_optimize`

`CLAUDE.md` declares that module "the optimize gate's **PRESENTATION** half — every markdown block the
skill prints". Six blocks had no renderer because the fences hand-formatted them with `print(...)`, and
four more notes had no block at all — the staleness warning, the attribution fallback, the
family-shrink notice and Stage C's family size, each of which a composite would otherwise have written
itself. **Ten** new `render_*` functions landed there rather than in `api.py`; the plan predicted five,
and the gap is what the boundary actually cost once it was enforced rather than intended. **`api.py` authors no markdown**,
and that boundary was broken twice during implementation and caught both times in review — first by
`_staleness_note`'s bolded sentence, then by the Stage C family-size line. It is a harness gap: nothing
mechanically forbids a markdown literal in that one module.

## Why Stage C recomputes instead of persisting a verdict

`confirm_gate` needs the **Holm-corrected** Stage B verdict, because `promoted` is what Stage C
classifies against — and `measurements.json` is `extra="forbid"` with nowhere to put one. The
bootstrap is seeded, so re-gating the family and correcting again is bit-identical; the cost is CPU
over rows already on disk (measured: ~18 s on a five-candidate activation family, of which the
recomputation is ~85%). It also removes the `NameError`-after-payment failure the skill's own prose
warned about, because every input is an argument.

**And it must NOT refuse a candidate that merely lost.** The first implementation did, and rank 1 says
otherwise in writing: `gate.confirm_train_note`'s docstring is *"A NOTE, not a refusal: a reader may
legitimately want to confirm a candidate that separated and was then vetoed by a guardrail."* A rank-4
composite whose contract is that it decides nothing was deciding that the other way, and made both
rank-1 helpers unreachable from the only surface the skill uses. It now refuses only a verdict with no
statistic at all — a gate that could not measure is not a candidate that lost.

## Why per-track functions and never a `track:` discriminator

The library splits by track everywhere (`confirm_gate` vs `confirm_gate_execution`, `holm_promote` vs
`holm_promote_execution`), so Stage B, Stage C and the ledger each get two composites. One function
with a `track:` literal would carry mutually exclusive parameters, force an un-typeable signature, and
need a runtime assert for a combination the split makes unrepresentable — a grader fingerprint on the
activation track. `record_round_activation` simply has no grader parameter, which is the whole point.

The test that pins this asserts the **two entry points and their disjoint parameter sets**, not a grep
for the string `track:`. A grep was the first attempt and it was decoration: the module legitimately
carries `_track_verdict(…, track_name)`, so renaming a parameter satisfied it without the design
holding.

## The one fail-open case, and why it is stated rather than prevented

A verdict with no p-value is not a Holm family member, so an arm that refused drops out and `m` falls.
Right for that arm, **wrong for its siblings**: they were predeclared against the larger family and are
decided against the smaller, looser threshold. Measured: two mapping keys pointing at one run dir
promoted the good arm "across a family of 1" while the round had predeclared two. Every other guard in
this area fails closed; this one fails open, and only a caller holding the predeclared count can see
it. `render_family_shrunk` says so on all four Stage B / Stage C surfaces.

## Declined: generating `reference/run-layout.md` from `.claude/shared/run-layout.md`

Recorded so it is not re-proposed. `tests/lint_tests/test_lint_plugin_skills.py` already considered
generation and rejected it: *"Generating one hand-written file from another would add machinery
without adding a source of truth, so this byte-equality assert is the sensor instead."* That is
correct. `reference/criteria.md` is generated because it is **derived from the models**;
`run-layout.md` is hand-written prose on both sides, so a generator would be a `cp` with a Makefile
target.
