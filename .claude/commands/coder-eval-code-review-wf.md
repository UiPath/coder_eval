---
allowed-tools: Bash(*), Read(*), Grep(*), Glob(*), Write(tmp/code-review*/*), Workflow
description: Workflow-based 8-axis codebase review — per-axis sub-workflows, adversarial verify, deterministic scoring + rendering
---

## Context

This is the **workflow-orchestrated** variant of `/coder-eval-code-review-full`.
It produces the *same* report (same axes, severity standard, scoring formula,
output files, PR comment) but replaces the inline parallel-`Agent` fan-out
(steps 3–5 of the sibling command) with a single `Workflow` call:

- **One sub-workflow per axis** (Option B). The parent workflow invokes a
  parameterized `cr-axis` child once per selected axis, so each axis is an
  isolated, independently-resumable sub-run.
- **Each axis agent reads its rubric from the sibling file directly.** The
  Review Principles / Severity Standard / Output Format / Techniques are NOT
  pasted into the workflow payload — the agent `Read`s them from
  `.claude/commands/coder-eval-code-review-full.md`. This keeps a single literal
  source of truth and keeps the `args` payload small (just paths, per-axis
  starting points, and routed tool output).
- **Adversarial verification of every medium+ finding.** Inside each axis
  sub-workflow, every 🔴/🟠/🟡 finding is independently re-checked by a second
  agent whose job is to *refute* it. False-positives are **dropped**; a finding
  that is real but has an inaccurate detail (wrong line count, metric, symbol)
  is **corrected** in place (the verifier returns a `corrected_title` carrying
  the verified facts). 🔵 (low) findings pass through unverified. A
  **verification ledger** (proposed / verified / refuted / corrected counts +
  the refuted list with reasons) is surfaced in the report and `results.json`,
  so false-positive rate is measurable and comparable run-over-run.
- **Dedup + theme-group before scoring.** One synthesis agent merges findings
  that are the same root cause (incl. the same issue surfaced under two axes) and
  collapses same-class themes (e.g. several god-functions) into one scored
  finding that lists its members — so a single issue isn't counted multiple
  times and one theme can't tank an axis. Scoring runs on the merged set.
- **Deterministic scoring AND rendering in JS.** Counts → score, overall mean,
  weakest axis, and cross-axis convergence are computed in the workflow script;
  the workflow also **renders the report markdown itself** and returns a
  `files` map (filename → contents), including a machine-readable
  `results.json` (structured findings + scores for trend/external use). The main
  agent writes those verbatim — no hand-transcription, so the report can't drift
  from the data.

Everything else — scope resolution, the worktree, the automated tool checks,
saving raw tool output, and PR-comment posting — stays in the main loop (this
command), because those are one-time I/O and shared-state actions that belong
under the main agent's direct control.

**Opt-in note:** invoking `Workflow` is explicitly part of this command's
instructions (step 5 below), which is what authorizes the tool call. Do not ask
the user to re-confirm.

Optional argument: `$ARGUMENTS` — parsed **identically** to
`/coder-eval-code-review-full` (see its **Scope Selection**), plus one extra
toggle:

- `--no-verify` — skip the adversarial verification stage (faster, cheaper;
  every finding is reported as-is). Default is **verify on**.

## Single source of truth

The axis and synthesis agents **read the canonical command's sections at
runtime** rather than receiving a pasted copy. You pass the **absolute path** to
`.claude/commands/coder-eval-code-review-full.md` as `shared.siblingPath`; the
agent prompts (baked into the scripts) instruct each agent to `Read` the
sections it needs by heading:

- **Review Principles**, **Severity Standard** (incl. the per-axis anchor table,
  the Axis-4 CVSS requirement, and the Axis-8 scoring-correctness rule),
  **Output Format**, and the **Techniques to apply** block → read by every axis
  agent.
- The **What's Missing** and **Harness & Lint Improvements** synthesis-pass
  bullets (under step 5 of the sibling's Procedure) → read by the two synthesis
  agents.
- **Axis starting points** — passed per-axis in the `axes` payload (small), so
  each agent gets its own entry point without scanning the file.

The scoring formula is pinned in the workflow script and **must stay in sync**
with the sibling's Scoring section:
`score = max(0, 10 − 3.0·🔴 − 1.0·🟠 − 0.5·🟡 − 0.1·🔵)`.

## Procedure

1. **Resolve scope + capture review metadata** — exactly as the sibling's
   step 1 (parse `$ARGUMENTS`; resolve the in-scope file list, selected axes,
   `--post-comment` target PR, scope summary string). Detect `--no-verify` →
   `verify = false` (else `true`). Capture reproducibility metadata:
   `git rev-parse HEAD` (SHA), `git rev-parse --abbrev-ref HEAD` (branch), an ISO
   timestamp (`date -u +%Y-%m-%dT%H:%MZ`), and the model identifier — these go
   into `args.meta` so the workflow can render the metadata block.

2. **Prepare checkout, run automated checks (save raw output), discover
   packages** — as the sibling's steps 2, 2b, 3, with two additions:
   - Worktree for `pr:<N>` (or a `--post-comment`-resolved current-branch PR), as
     the sibling.
   - Create `<dir>/automated/` (see step 4 for `<dir>`) and run the tools,
     **tee-ing each tool's raw output to `<dir>/automated/<tool>.txt`** for
     traceability: `ruff` (check + format), `pyright`, `bandit -ll`, `pip-audit`,
     **`radon cc -s -a src/coder_eval/`** (cyclomatic complexity), **`pytest
     --cov=coder_eval --cov-report=term-missing -q`** (tests + coverage), and the
     CodeQL/code-scanning alert pull. Fall back to plain `pytest` if `--cov`
     isn't available.
   - Route the outputs to axes when you build `automatedSummary` in step 3:
     ruff → 1; pyright → 2; **coverage (term-missing per-file gaps) → 3**;
     bandit + pip-audit + CodeQL-security → 4; **radon CC F/E-grade offenders →
     1 and 5** (complexity feeds both code-quality and god-class/architecture);
     CodeQL control-flow → 6; CodeQL import-cycle → 5; CodeQL test-tautology → 3.
   - `ls src/coder_eval/` (and `ls tests/` if Axis 3 is selected) for the
     authoritative package list.

3. **Assemble the workflow payload.** Build a single JSON value (kept small —
   no verbatim rubric text; the agents read it themselves):

   ```
   meta = { sha, branch, timestamp, scope, model }   // for the rendered metadata block

   shared = {
     verify:        <true|false>,
     siblingPath:   "/abs/path/to/.claude/commands/coder-eval-code-review-full.md",
     scopeSpec:     "<e.g. all — whole-codebase review (N files); or pr:253 (12 files) axis:1,2,3>",
     fileList:      "<in-scope file paths (for `all`: a note that the whole package is in scope + the package tree)>",
     scopeRules:    "<for non-`all`: the two non-`all` scope rules; for `all`: a note that axes are judged in absolute terms>",
     prContentRule: "<for pr:<N>: 'read file contents via git show pr-<N>:<path>'; else empty string>",
     packages:      "<package list from step 2>"
   }

   axes = [ { num, name, startingPoint: "<verbatim Agent-N starting-point bullet>", automatedSummary: "<routed digest from step 2>" }, ... ]  // one per SELECTED axis
   ```

   Keep `automatedSummary` tight (a digest, not full dumps — the raw output is
   already saved under `<dir>/automated/`).

4. **Set up the report dir.** The workflow scripts are version-controlled — you
   do NOT write them per run; they live at `.claude/workflows/cr-parent.js`
   (orchestrator) and `.claude/workflows/cr-axis.js` (one axis: review +
   adversarial verify/correct of that axis's findings; the parent invokes it by
   name). Just create the timestamped output dir:
   - `date +%y%m%d-%H%M` → `<ts>`; report dir `tmp/code-review-<ts>/` (absolute
     under the repo root). `mkdir -p <dir>/automated/` for step 2.

5. **Run the workflow.** Invoke `Workflow` with:
   - `scriptPath`: the absolute path to `.claude/workflows/cr-parent.js` (resolve
     `<repo>/.claude/workflows/cr-parent.js` against the repo root — do not hardcode
     a machine-specific path).
   - `args`: `{ meta: <meta>, axes: <axes>, shared: <shared> }`. (No
     `axisWorkflowPath` — the parent references the `cr-axis` child by name.)

   The scripts normalize `args` whether it arrives as an object or a JSON string.
   The parent fans out one `cr-axis` sub-workflow per axis (each reads its rubric
   from `siblingPath`, reviews, then verifies/corrects its own medium+ findings),
   **de-duplicates/theme-groups the findings (one agent) and scores
   deterministically from the merged set**, runs three synthesis agents (What's
   Missing, Harness & Lint, and a verdict/Top-5 summarizer), and **renders the
   full report markdown plus a machine-readable `results.json`**. It returns:

   ```
   {
     scored, overall, weakest, findings, whatsMissing, harnessLint, summary,
     missingAxes,
     files: { "00-summary.md": "...", "01-code-quality.md": "...", ..., "99-pr-comment.md": "...", "results.json": "..." }
   }
   ```

   If `missingAxes` is non-empty, note it explicitly (those axes produced no
   results — do not treat them as clean).

6. **Write the report files** — the workflow already rendered them. For each
   `name → contents` entry in the returned `files`, `Write` `<dir>/<name>`
   verbatim — this includes `results.json` (structured findings + scores)
   alongside the markdown. **Do not reformat or recompute** — `scored` and the
   rendered files are authoritative. (The raw tool outputs from step 2 are
   already under `<dir>/automated/`.) After writing, print the resolved `<dir>`
   path. The `files` map can be large; if writing each entry inline is
   unwieldy, write a tiny extractor that parses the returned result and writes
   `files[name]` to `<dir>/<name>`.

7. **Print the executive summary to the screen** — assemble from the returned
   structured result (no re-derivation): `summary.verdict`; the Change-class line
   for non-`all` scopes; the Summary table (rebuild from `scored`); Overall /
   Weakest / Totals; the full Critical & High list (the `findings` with severity
   `critical`/`high`); the What's Missing list; the Harness & Lint list (static
   checks first); the **Verification** one-liner (`verification.totals`:
   verified / refuted-false-positive / corrected); `summary.top5`; and a trailing
   `Full report: <dir>` line. Cap ~80 lines; do not print 🟡/🔵 inline.

   Then, as the **final line(s)**, print a **Next step** recommendation about
   `/coder-eval-create-plan` (the planning skill that turns work into a
   structured implementation plan). Decide from the results:
   - **Fixes:** if there is any 🔴/🟠 finding (or 🟡 you consider pre-merge),
     recommend running `/coder-eval-create-plan` to turn them into a plan, and
     propose a concrete scope — usually `summary.top5`, or "the Blockers in
     `99-pr-comment.md`".
   - **Harness improvements:** if the **Harness & Lint Improvements** list is
     non-empty, recommend a *separate* `/coder-eval-create-plan` for those (the
     `CEnnn` lint rules + the harness tests) — they're durable-infrastructure
     work distinct from the point fixes, and are often the higher-leverage plan.
   - If the review is clean (no 🟠+ findings **and** an empty Harness & Lint
     list), say a plan isn't needed.
   Phrase it as a suggestion the user can accept or decline, with the exact
   invocation (e.g. `/coder-eval-create-plan fix the Top 5 priority actions from
   tmp/code-review-<ts>/00-summary.md`). **Do NOT invoke `/coder-eval-create-plan`
   automatically** — only recommend it.

8. **PR comment** — `<dir>/99-pr-comment.md` was rendered by the workflow. If
   `--post-comment` was set, run `gh pr review <N> --comment --body-file
   <dir>/99-pr-comment.md` against the resolved PR; otherwise suggest the
   one-liner (don't run it). Same rules as the sibling's step 8.

9. **Cleanup** — remove the PR worktree if step 2 created one
   (`git worktree remove --force …` + `git branch -D pr-<N>`). The report and
   `<dir>/automated/` stay; the workflow scripts live in `.claude/workflows/`
   (version-controlled), not in the run dir. Skip for non-PR scopes.

   **Resuming:** if interrupted, re-invoke `Workflow` with
   `scriptPath: <repo>/.claude/workflows/cr-parent.js`, `resumeFromRunId: <runId>`,
   AND the same `args` (args are not persisted across runs) — completed axis
   sub-workflows return cached results.

## Workflow scripts

The two scripts are version-controlled at `.claude/workflows/cr-parent.js`
(orchestrator: fan-out → dedup/theme-group → score → synthesize → render) and
`.claude/workflows/cr-axis.js` (one axis: read the rubric from the sibling,
review, then **validate** — adversarially verify/correct every medium+ finding
and drop false-positives). Edit them there; they are plain JavaScript and are
syntax-checkable directly (wrap in an `async` function to satisfy top-level
`await`/`return`). There is no separate "validate findings" file — validation is
the Verify phase inside `cr-axis.js`.
