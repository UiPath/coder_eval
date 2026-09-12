# Pinned per-harness timing corpus

One scrubbed, representative `task.json` per harness, from a real
`tasks/timing-parallel-tools` run. Prompts, outputs, tokens and cost are
stripped; the wall-clock fields `scripts/timing/decompose_run.py` reads survive,
plus the handful `TurnRecord` requires (`user_input`, `agent_output`, each
command's `timestamp`) as neutral placeholders — the script validates each turn
into the model so it can call production's own span selector, and the original
scrub had left these records below what that requires.

**NO TEST MAY READ THIS DIRECTORY.** That is why it lives under `scripts/` and
not under `tests/_fixtures/`. Two of the five records deliberately preserve
defects the live code no longer has (see below); a green assertion over them
would pin a fixed defect as expected behaviour, and a reader who finds
stale-by-design data under `tests/` has every reason to point a test at it.

```
uv run python scripts/timing/decompose_run.py scripts/timing/corpus/*.json --min-turn-ms 0
```

## What this is NOT

**It cannot show a before/after for a code change, and it was originally asked
to.** `decompose_run.py` READS STORED FIELDS — `harness_startup_ms`,
`generation_duration_ms`, the command bounds — out of a recorded record. It
recomputes nothing from `src/`. Run over a fixed corpus it prints the identical
table before and after any change to the harness, so a green result would be
meaningless rather than reassuring.

Two rows here make that concrete:

- **claude-code reconciles at −481 ms (−2.69%).** That is the pre-subtraction
  defect `_subtract_tool_time_from_windows` was written to fix, frozen in a
  record written before the fix landed. The live code has not had that defect
  for some time.
- **claude-code and antigravity both book a `0.0` head.** That is the clamped
  inversion the "one meaning for `harness_startup_ms`" change removed. Neither
  harness produces it any more.

Re-recording the corpus after a change would fix both, and would also destroy
the only thing the corpus is good for.

## What it IS

A reproducible statement of what real runs of each harness look like — the
shape of the buckets, the per-harness spread in the head, and a fixed input for
`decompose_run.py` itself. It is what the timing audit's P0 was read off: the
two harnesses reporting a `0.0` head had a FIRST generation window 2.4–3.8× their
own later median, and that excess was the startup they were not booking.

## The instruments that DO move with the code

- `tests/test_timing_identity_contract.py` — drives each of the five reducers
  off a scripted clock and asserts `head + Σgeneration + UNION(tool) + tail`
  equals the turn span to the millisecond. This is the sensor for the
  arithmetic.
- `tests/test_agent_golden_master.py` — replays recorded streams end to end.
  Note it masks every timing VALUE (`_scrub.py::SCRUB_KEYS`), so it sees shape
  and not magnitude.
- `scripts/timing/decompose_run.py --max-residual-pct` over a **fresh** run,
  which `.github/workflows/pr-checks.yml` does against the smoke-pass bucket.
  A live run is the only way this script can say anything about current code.
