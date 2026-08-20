# Instrument provenance: what the two fingerprints cover, and why there are two

Subject: `suite_fingerprint.py`, `models/optimize.py::RoundScores.suite_fingerprint` /
`grader_fingerprint`, `optimize/store.py::suite_changed` / `grader_changed`.

## Why a second fingerprint was needed

The grader fingerprint covers the outcome track's script and answer key. It cannot see a `weight`
change that re-blends `weighted_score` — the very number the execution gate's paired *t* compares —
and the activation track has no script grader at all, so it had NO instrument provenance of any kind.
A round's numbers were comparable across rounds only by assumption.

## The ROWS are load-bearing, not decoration

`activation.yaml` is `initial_prompt: ${row.prompt}` with `expected_skill: "${row.expected_skill}"`,
so every prompt AND every label lives in the rows file. A digest over row IDS alone — which is what
the first implementation did — is blind to a rewritten prompt and a flipped label: the commonest
suite edit there is, on the track this digest is the sole provenance for.

## A DENYLIST, not an allowlist, and this is the one place that is the safe direction

`scoring_dump` minus `_NOT_SCORING_RELEVANT` (a reason-carrying denylist of exactly one field,
`description`). An allowlist over `BaseSuccessCriterion`'s five fields silently omits every subclass
parameter — which is how the first draft could not see `run_command.command` move from `verify.py` to
`verify2.py`.

## `run_limits` is hashed WHOLE

Rather than through four curated caps: the three token caps abort a run exactly as `max_usd` does,
and `stop_early` is the kill switch for every armed criterion, so it moves `f1.yes` itself.

## Length-prefixing and section tags are REDUNDANCY here

Both are present, as in `verify.py::fingerprint`, but `_canonical` — canonical JSON per part — is
what actually stops a value forging a delimiter or a part migrating between sections. A mutation test
proved the obvious attribution wrong.

## Order sensitivity is deliberate and asymmetric

Order-SENSITIVE across criteria, because `criterion_index` is positional everywhere in this family.
Order-INSENSITIVE within a mapping and across rows.

## What it excludes, and the one boundary that follows

The TASK-LEVEL agent and sandbox blocks, so two checkouts agree. One stated consequence: an
`agent_judge` criterion embeds its own agent config and is hashed whole — which is right, since the
judge's model is part of what it measures, and which makes such a suite's digest machine-local.

## Digest only, never the pre-image

`measurements.json` is committed.

## Three-valued, both of them

`suite_changed` and `grader_changed` return "changed" / "unchanged" / "cannot tell" rather than a
bool. A round with no recorded fingerprint is not a round whose instrument matched.
