# Task quality rubric

Two readers use this file: `/coder-eval:task` applies it to work it just wrote, and
`/coder-eval:lint-tasks` applies it to task files already on disk. It declares **checks
only** — how to rank or report what you find is the reviewing skill's business, not this
file's.

A task is an instrument. These checks ask whether the instrument measures what its
description claims, or whether it measures nothing and reports a number anyway.

## 1. Could this pass for the wrong reason?

The framing question, asked before anything else:

> **What is the cheapest thing an agent could do that scores full marks?**

Answer it out loud. If the cheapest path does not resemble the work the task claims to
test, the criteria are wrong — not the agent. Then walk the six mechanical checks:

| # | Check | Fix |
|---|---|---|
| 1 | Does a `command_executed` criterion credit an invocation that **failed**? | Set `require_success: true` whenever the command's success is what is being graded. The default is `false`, which counts a crashed invocation as evidence the work was done. |
| 2 | Does the pattern also match a help probe, or a longer word that contains it? | Add `exclude_pattern` and word boundaries. Cover **both** `--help` and `-h`; an agent that runs `tool --help` and gives up otherwise satisfies a bare `tool` pattern. |
| 3 | Would an agent that does **nothing** pass? | Total the `weight` of every criterion satisfiable by inaction — `min_count: 0`, a `max_count: 0` negative, a `file_exists` on a file the setup already created. If that total alone clears `pass_threshold`, the task is a no-op detector. |
| 4 | Does a regex alternation launder the assertion? | An alternation branch that makes the payload opaque — matching an indirection flag such as `--from-file` instead of the content that flag points at — proves nothing. Keep the shape criterion on the shape-visible form and add a companion check on the payload itself. |
| 5 | Does `min_count` express a *count* where the description promises distinct *content*? | Assert the distinct content. Three calls to the same endpoint satisfy `min_count: 3` and demonstrate none of the coverage the description claims. |
| 6 | Is the end state reachable without the system under test? | Ask whether local file manipulation alone could satisfy every criterion. If an agent could hand-write the expected output instead of calling the tool, the tool is not being tested. |

*Seen in the wild:* every one of these has shipped in a real suite and passed review.
Check 1 is the most common by a wide margin — the field defaults to the permissive value,
so it is what you get by not thinking about it. Check 3 is the most expensive, because a
no-op detector reports a healthy score forever and nobody looks at it again.

## 2. Grade behaviour, not self-reports

A criterion that reads a file the prompt asked the agent to write *about its own work*
grades a claim, not an outcome. "Write a summary of the changes you made to `report.md`"
plus a criterion checking `report.md` mentions the change tests whether the agent can
describe itself, which it can.

Grade the artifact the work produced, or the command that produced it. If a self-report is
genuinely the deliverable, then something else must independently establish the facts it
reports.

*Seen in the wild:* a task whose only content check read the agent's own changelog entry.

## 3. Judges complement, they do not carry

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

## 4. Scope match

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

## 5. Fixture lifecycle

**This section is the canonical home for these checks.** Other skills point here rather
than restating them.

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
