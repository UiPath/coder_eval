# Task quality rubric

**Consumers** (keep this list current when you add one):

- `/coder-eval:task` — applies it to work it just wrote.
- `/coder-eval:lint-tasks` — applies it to task files already on disk.
- this repository's own `coder-eval-task-create` contributor command.

It declares **checks only** — how to rank or report what you find is the reviewing skill's
business, not this file's.

A task is an instrument. These checks ask whether the instrument measures what its
description claims, or whether it measures nothing and reports a number anyway.

## 0. First: what is this task's subject?

Nearly every check below assumes the task measures **an agent's capability**. Some tasks
deliberately do not — they exercise the evaluation framework itself: that a dataset expands,
that a budget cap trips, that a template lands in the sandbox, that a judge is wired up. For
those, several checks below turn into false alarms. Establish the subject before applying them.

Signals that a task's subject is the framework rather than an agent: `agent: {type: none}`; a
`smoke`-style tag; a criterion whose satisfaction is produced by `pre_run`, by the container
image, or by `template_sources`; or a description that names a framework mechanism instead of
a capability.

For those tasks:

- **Check 3 does not apply.** Reading back what the setup wrote *is* the measurement — a
  criterion satisfiable by "inaction" is the correct design, not a no-op detector.
- **A prompt that names the exact string a criterion greps for is correct**, not the
  can't-fail trap. Proving substitution or redirection works requires a known string on both
  ends.
- **The judges section's "the judge must not be the only signal" does not apply** when the judge is the thing
  under test. Its variance warning still does.

Everything else still applies, and one extra check applies only here: **does the mechanism
this task claims to exercise still exist?** A fixture whose feature was removed fails forever
while appearing to test something.

## 1. Could this pass for the wrong reason?

The framing question, asked before anything else:

> **What is the cheapest thing an agent could do that scores full marks?**

Answer it out loud. If the cheapest path does not resemble the work the task claims to
test, the criteria are wrong — not the agent. Then walk the mechanical checks:

| # | Check | Fix |
|---|---|---|
| 1 | Does a `command_executed` criterion credit an invocation that **failed**? | Set `require_success: true` whenever the command's success is what is being graded. The default is `false`, which counts a crashed invocation as evidence the work was done. |
| 2 | Does the pattern also match a help probe, or a longer word that contains it? | Add `exclude_pattern` and word boundaries. Cover **both** `--help` and `-h`; an agent that runs `tool --help` and gives up otherwise satisfies a bare `tool` pattern. |
| 3 | Would an agent that does **nothing** pass? | List the criteria satisfiable by inaction — `min_count: 0`, a `max_count: 0` negative, a `file_exists` on a file the setup already created. A task passes only when **every** scoring criterion (`weight` above 0) meets its own `pass_threshold`, so if inaction satisfies all of them the task is a no-op detector. If it satisfies only some, total their `weight` against the task's: that share is how much of the score is free. |
| 4 | Does a regex alternation launder the assertion? | An alternation branch that makes the payload opaque — matching an indirection flag such as `--from-file` instead of the content that flag points at — proves nothing. Keep the shape criterion on the shape-visible form and add a companion check on the payload itself. |
| 5 | Does `min_count` express a *count* where the description promises distinct *content*? | Assert the distinct content. Three calls to the same endpoint satisfy `min_count: 3` and demonstrate none of the coverage the description claims. |
| 6 | Is the end state reachable without the system under test? | Ask whether local file manipulation alone could satisfy every criterion. If an agent could hand-write the expected output instead of calling the tool, the tool is not being tested. |
| 7 | Does a criterion match a literal the **prompt already dictates**? | Then it cannot fail — the agent was told the answer, and you are grading transcription. Either the literal is a real requirement (keep it in the prompt and score what the agent *did with it*) or it is the thing under test (drop it from the prompt). Never both. See section 0 first: for a framework-plumbing task a dictated literal is the correct design. |

*Seen in the wild:* every one of these has shipped in a real suite and passed review.
Check 1 is the most common by a wide margin — the field defaults to the permissive value,
so it is what you get by not thinking about it. Check 3 is the most expensive, because a
no-op detector reports a healthy score forever and nobody looks at it again.

## 2. Does anything check the output's *content*?

At least one criterion must inspect what the output **says**, not merely that it exists. A
suite of `file_exists` checks passes when the agent writes an empty file, and `touch out.json`
satisfies every one of them.

Exempt (see section 0): a framework-plumbing task whose subject is that a file arrives at all,
and a classification suite whose rows have no artifact to inspect — there the aggregate across
rows is the content check.

## 3. Grade behaviour, not self-reports

A criterion that reads a file the prompt asked the agent to write *about its own work*
grades a claim, not an outcome. "Write a summary of the changes you made to `report.md`"
plus a criterion checking `report.md` mentions the change tests whether the agent can
describe itself, which it can.

Grade the artifact the work produced, or the command that produced it. If a self-report is
genuinely the deliverable, then something else must independently establish the facts it
reports.

*Seen in the wild:* a task whose only content check read the agent's own changelog entry.

## 4. Judges complement, they do not carry

`llm_judge` and `agent_judge` are the right tool for open-ended quality and the wrong tool
for anything a deterministic criterion can check. Two failure shapes:

- **The judge is the only signal.** A single `llm_judge` at high weight means the task's
  verdict is one model's opinion, re-rolled on every run. Add deterministic criteria for
  everything checkable and let the judge grade only the part that genuinely needs judgement.
- **The rubric conjoins N conditions at `pass_threshold: 1.0`.** This is the
  highest-variance shape available: every condition must land in one generation, so the
  score swings on wording. Split it into one criterion per condition, each independently
  scored and weighted.

*Seen in the wild:* a six-clause judge rubric at `pass_threshold: 1.0` that scored
anywhere from 0.4 to 1.0 across identical runs.

## 5. Scope match

Read `initial_prompt` and `description` against the criteria as a set:

- Everything the prompt **asks for** should be graded. An ungraded instruction is either
  dead prompt text or a silent coverage hole.
- Everything the description **claims** should be exercised. A task described as testing
  error handling whose criteria only check the happy path is mis-labelled, and the label
  is what people trust when they read a suite summary.

Deleting a criterion without trimming the prompt is the usual way this drifts — the prompt
still asks, nothing still checks.

*Seen in the wild:* a task whose prompt asked for three output files after the criteria for
two of them had been removed as flaky. It scored 1.0 while testing a third of its subject.

## 6. Fixture lifecycle

**This section is the canonical home for these checks.** The skills that *apply* this rubric
point here rather than restating them. (`/coder-eval:analyze` separately diagnoses the same
failure from the other end — a finished run's residue signature — which is a different job
from reviewing a task file, so it states its own version.)

It fires only when a task touches state **outside** the sandbox — a tenant, a hosted
service, a shared account, a remote registry. A task confined to its own temporary
directory is exempt: the sandbox is the reset.

- **`pre_run` resets to a known state; `post_run` cleans up.** Removing either is how a
  task self-poisons: one bad run leaves residue, and the criterion then fails on every
  subsequent run forever. A task that passes once and fails permanently afterwards is the
  signature.
- **Grade "the agent did it *this run*", not "the state is right now."** End-state grading
  false-passes when something else already produced the state, and false-fails when
  something else undoes it mid-run. Gate on evidence the agent itself produced — its
  commands, its output files — and keep the end-state re-read as a complementary,
  lower-stakes check.
- **Run-unique fixture names.** A fixed name collides with a concurrent run and inherits
  stale residue from a crashed one. A date-stamped name is a throwaway that accumulates
  inside a permanent account.
- **Cleanup must be reachable on the failure path.** Record each identifier as it is
  created, not after the last one succeeds — otherwise a failure mid-sequence orphans
  everything created before it. `post_run` commands do not affect pass/fail, so they still
  run when the task fails; that is what makes them the right place for teardown.
- **Ordering contract.** Experiment-defaults `pre_run` runs first (baseline setup), then
  the task's. On the way out the task's `post_run` runs first, then experiment-defaults
  `post_run` — cleanup last. Setup that a later task depends on belongs in the experiment
  defaults; anything run-specific belongs on the task.
- **A failing `pre_run` command aborts the evaluation** by default, which is usually what
  you want: grading against an unprepared fixture produces a number that means nothing.
  Set `fail_on_error: false` only for a command whose failure is genuinely informational.

*Seen in the wild:* a suite where one task created a fixture under a fixed name and never
deleted it, so every subsequent run of the whole suite failed on the create step.
