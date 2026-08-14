# The reflective proposal

The shape of the prompt that generates candidate edits. Not the method (what a gate proves) and
not the procedure (which command runs when) — just what the proposer is given, what it is
deliberately not given, and what it is told to produce.

Use it whenever you are about to write candidates. A proposer handed only scores writes
plausible-sounding rewrites; a proposer handed the *trajectories* writes edits that answer
specific failures.

## What the proposer receives

**The failing rows, with what actually happened on them.** Not the aggregate — the rows. For
each one: the prompt that produced it, the expected outcome, the observed outcome, and the
trajectory from that row's `task.json` — the `commands` on each entry of its `iterations`,
in order. A score says a row failed. The
trajectory says *which instruction the agent followed instead*, and that difference is the whole
content of a good hypothesis.

**Every previous attempt, with its train score.** Candidate text, the hypothesis it embodied, and
what it measured. Then the instruction that makes the history worth carrying:

> Do not repeat these. Try something **structurally different** — a different theory of why the
> failures happen, not a reworded version of a theory that already failed to move the number.

Without that line a proposer converges: round three produces a paraphrase of round one, the
numbers agree with round one, and the loop spends money confirming a dead end.

**The incumbent, exactly as it stands**, so a candidate is a diff and not a rewrite from memory.

## What the proposer is NOT given

**The test rows, and any score computed on them.** The proposer is **blinded** to the test split.

This is not a formality. Every proposal is fitted to what the proposer can see, so anything it
sees becomes training data — and the test split's only job is to be rows the candidate was never
fitted to. Show the proposer a test score and the split silently becomes a second train split:
the confirmation stage still runs, still renders, still reports a number, and that number no
longer means what the report says it means. There is no way to detect this afterwards from the
artifacts, which is why it has to be prevented here.

The same applies to a summary of the test rows, their themes, or "roughly what they cover".

## What the proposer produces

One candidate per hypothesis, each a **single named change** with the failing rows it is meant to
fix named beside it. A candidate that cannot say which rows it explains is not testable, and a
candidate that changes several things at once cannot be partially kept when one part regresses.

**Generalize to categories of user intent — never to an expanding list of specific queries.** The
tempting fix for a missed row is to add its wording. That scores well on the row it was written
for and nowhere else, which is overfitting in its purest form: the suite improves, the skill does
not. Ask instead what *kind* of request the miss belongs to, and whether the edit would catch a
request from that kind that nobody has written down yet.

## Track-specific constraints

**Activation track — budget the length before writing.** Every natural fix here lengthens the
description: widening vocabulary, naming the symptom, spelling out an exclusion. Descriptions are
subject to a per-skill truncation *and* a whole-listing budget shared with every skill the user
has installed. Measure the current total and the headroom first and hand the proposer a character
ceiling, rather than discovering after the A/B that the winner cannot be loaded. Where a candidate
needs room, buy it by cutting what the body already covers — never a trigger clause.

**Execution track — same structure, body in place of the description.** The proposer receives the
body under test instead of the frontmatter, and the trajectories matter more rather than less,
because the failure modes are about instructions the agent read and did not follow. The
smallest-edit preference still governs: prefer the least change that could plausibly fix the
failing rows. A wholesale rewrite may well score better and teaches you nothing about why, and it
cannot be partially reverted when one part of it regresses a row that used to pass.
