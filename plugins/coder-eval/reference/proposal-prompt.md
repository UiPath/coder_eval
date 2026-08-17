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

**The arm this candidate will be edited FROM, exactly as it stands**, so a candidate is a diff and
not a rewrite from memory. On a multi-arm round that is the **incumbent**; on a search round it is
the **lineage head**, which is the accumulated text the loop is carrying forward. Handing the
proposer the incumbent on a search round silently discards everything the lineage accumulated and
leaves the skill's own leak check — which diffs against *what the candidate was derived from* —
with nothing coherent to compare against.

**The gold solution, where the suite carries one — and it is a different input from the two
above.** The task's `reference:` block holds a reference solution for `reference_comparison`,
`llm_judge` and `agent_judge`; a row may also carry the shape of a correct answer in a field its
criteria assert on, as the bundled outcome template does with `expected_snippet`. Hand the
proposer those, and ask a question the expected-vs-observed pair cannot answer: *how was this row
meant to be solved?* Expected-vs-observed says the row failed and what came out instead. The
reference says what a correct result looks like, and the transferable thing is the **procedure**
that produces it — the step the baseline never told the agent to take, the ordering it never said
was load-bearing.

**Giving it to the proposer is legitimate; giving it to the agent would not be.** `reference:` is
hidden from the agent under test by design and never enters its prompts. The proposer is a
different actor working between rounds, so the blinding that matters to it is the *test split*
above — not the reference. Keep those two rules apart: relaxing the wrong one silently destroys
the confirmation stage.

Extract the procedure, never the answer. Writing the reference's content into the candidate is
the memorization the rule below forbids, and it is especially tempting here because the content is
sitting right there and it is *known correct*. A skill that carries one row's answer scores that
row and teaches nothing.

**And this is the one leak no checker will catch for you.** `candidate_leaks` scans what the
row's *criteria* assert on; the reference is a task-level field and is outside it, by that
function's own stated boundaries. So the gold solution is simultaneously the most tempting thing
to copy and the only one with no mechanical backstop. Re-read the candidate against the reference
yourself before snapshotting it.

**Scoped, and a no-op where it does not apply.** Most activation suites carry no reference at all
— `skill_triggered` grades an event, and there is nothing for a reference to be. Say so and move
on; its absence is not a blocker and not something to go author.

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

**A strategy specific to the failure's CATEGORY, carried on each edit.** The rule above says what
to generalize *to*; this says what the edit must then contain. Classify each failure by the *kind*
of error it is — the taxonomy in the skill's Step 7 names them per track — and require the edit to
answer that kind with a technique, not with emphasis. "Name the symptom vocabulary a user would
actually type" is a strategy; "make the description clearer" is a wish. "Give a worked example of
the output shape" is a strategy; "be more specific" is a wish. The test is whether a reader could
tell you had applied it by looking at the diff.

The reason to insist is that a category and a technique fail differently, and only the pair is
diagnostic: an edit that names its category but not its technique cannot be executed, and one that
names its technique but not its category cannot be checked against the rows it claims to fix.

**Never reproduce a train row's graded content verbatim.** The failing rows come with the strings
their criteria assert on, and the cheapest way to make a row pass is to paste one into the
candidate. It is overfitting at its most literal — the row passes whether or not the behaviour
under test happened, and an A/B arm that *deleted* that behaviour would pass it too. Write the
*procedure* that produces the content, never the content.

**Validation, before the candidate is snapshotted.** Review every generated candidate for train-row
content it reproduces word for word. The mechanical half is
`coder_eval.optimize_search.candidate_leaks(candidate_text, baseline_text, train_rows)`, which
reports the substantive spans the candidate adds and the text it was edited from lacks; the skill's
Step 8 runs it.

**The reader's half is not optional, and it covers two things the checker cannot.** A candidate
that describes a row's graded content in different words leaks just as much and nothing sees it.
And because the check is a *diff*, a leak that once rode into a promotion is part of the baseline
from then on and is never reported again — so the round that introduces a span is the only round
that can catch it mechanically. Read the candidate, not just the checker's output.

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
