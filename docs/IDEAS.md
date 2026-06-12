# Ideas & Backlog

A lightweight, stack-ranked list of improvements for `coder_eval` (the eval harness,
the dashboard/CI, and the evalboard UI). Anyone can add an idea — keep it short, then
we triage and work the list top-down.

## How to stack rank

**Rank lives in one place: row order in the table below. Top row = do first.**
To re-rank, just move a row up or down — nothing else changes.

- Each idea has a **stable ID** (e.g. `expected-turns`) that never changes, even when
  the rank does. Headings and cross-references use the ID, not a position number, so
  reordering never means renumbering.
- To decide *where* a row goes, score it on **Impact** and **Effort** (1–5 each), then
  let **Score** rank it:

  > **Score = Impact + (6 − Effort)** — range 2–10, higher floats up. Sort by Score;
  > break ties with judgment (dependencies, momentum, who's free).

  - **Impact** = how much this helps the *skills people are building* or our
    organization's *ability to improve* them. Bigger lever = higher.
  - **Effort** = order-of-magnitude **tokens a coding agent burns to build it**
    (best guess is fine — it's a log scale, so just pick the nearest power of ten).

| Field | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|
| **Impact** | trivial | minor | useful | important | game-changing |
| **Effort** (tokens) | ~100K | ~1M | ~10M | ~100M | ~1B |

**Status:** 💡 idea · 🔍 scoping · 🚧 in progress · ✅ done · ❄️ parked

## Backlog

| ID | Idea | Impact | Effort | Score | Status | Owner |
|----|------|:------:|:------:|:-----:|:------:|-------|
| [`expected-turns`](#expected-turns) | Top-level Expected Turns score | 4 | 2 | 8 | 🚧 | Sherif |
| [`efficiency-columns`](#efficiency-columns) | Surface already-computed efficiency metrics on the board | 4 | 2 | 8 | 💡 | — |
| [`config-fingerprint`](#config-fingerprint) | Per-run config fingerprint + regression attribution | 4 | 2 | 8 | 💡 | — |
| [`judge-ensemble`](#judge-ensemble) | Multi-sample judge voting + verdict variance | 4 | 2 | 8 | 💡 | — |
| [`cost-preflight`](#cost-preflight) | Pre-flight budget validation + judge cost estimate | 3 | 1 | 8 | 💡 | — |
| [`long-failing`](#long-failing) | PR a fix for long-running failed tests | 4 | 3 | 7 | 💡 | — |
| [`failure-mode-rollup`](#failure-mode-rollup) | Failure-mode taxonomy headline | 4 | 3 | 7 | 💡 | — |
| [`judge-calibration`](#judge-calibration) | Golden-set judge calibration + drift detection | 4 | 3 | 7 | 💡 | — |
| [`result-cache`](#result-cache) | Skip-unchanged task result cache | 4 | 3 | 7 | 💡 | — |
| [`smart-judge-truncation`](#smart-judge-truncation) | Smarter judge context truncation | 3 | 2 | 7 | 💡 | — |
| [`per-criterion-telemetry`](#per-criterion-telemetry) | Per-criterion timing + token telemetry | 3 | 2 | 7 | 💡 | — |
| [`top-offenders`](#top-offenders) | Top Offenders page | 3 | 3 | 6 | 💡 | — |
| [`diff-criterion`](#diff-criterion) | Diff/patch validation criterion | 3 | 3 | 6 | 💡 | — |
| [`ux-revamp`](#ux-revamp) | UX revamp | 4 | 4 | 6 | 💡 | — |

---

### expected-turns
**Top-level Expected Turns score**

**What:** Roll today's *per-task* expected-turns signal up into a single headline
metric for a run (e.g. "% of tasks within expected turns", or mean turn overage),
surfaced on the dashboard and in the nightly Slack summary alongside pass-rate.

**Why:** A task can pass but take far more turns than budgeted — a real efficiency
regression that the pass/fail headline hides. We already compute the per-task signal;
we just don't aggregate or surface it at the top.

**What exists today:**
- `expected_turns_overage(result)` and `visible_turn_count(result)` —
  [src/coder_eval/reports_stats.py:174](../src/coder_eval/reports_stats.py#L174)
- Per-task "expected_turns exceeded" badge in the HTML report —
  [src/coder_eval/reports_html.py:341](../src/coder_eval/reports_html.py#L341)
- `run_limits.expected_turns` is the per-task budget source.

**Rough approach:** aggregate overage across a run → add a field to the run summary →
render on the evalboard + add one line to the nightly Slack summary.

**Open questions:**
- What's the right headline statistic — % within budget, mean overage, or count exceeded?
- Per-skill breakdown too, or run-level only to start?

---

### long-failing
**PR a fix for long-running failed tests**

**What:** Identify eval tasks/tests that consistently run long *and* fail (burning the
nightly's 4h budget for no signal), then open PRs to fix or quarantine them.

**Why:** Long failing tasks eat wall-clock and cost on every nightly run and add noise.
Tightening or fixing them improves both run time and the trustworthiness of the headline.

**Rough approach:** mine recent `runs/` for tasks with high duration + non-SUCCESS status →
rank worst offenders → per-task, either fix the underlying issue or adjust limits/quarantine →
PR each fix.

**Open questions:**
- Threshold for "long-running" — absolute seconds, or top-N by duration?
- Fix vs. quarantine vs. limit-tuning — decide per task or set a default policy?
- One-off cleanup, or a recurring report we act on each cycle?

---

### top-offenders
**Top Offenders page**

**What:** A single page that ranks the things most worth addressing first — worst
tasks/skills by failure rate, turn overage, duration, and cost — so triage starts from
"here's what to fix" instead of scrolling a full run.

**Why:** Today you read the whole board to find problems. A focused "top offenders"
view turns the metrics we collect (and the ones in `expected-turns` and `long-failing`)
into an actionable work queue, and gives this backlog a data-driven feed.

**Rough approach:** aggregate per-task/per-skill stats across the latest run (reuse the
signals from [reports_stats.py](../src/coder_eval/reports_stats.py)) → rank by a few axes →
render as a sortable list on the evalboard, linking each row to its task detail.

**Open questions:**
- Which axes and what default sort — failures first, or a blended "needs-attention" score?
- Latest-run only, or trend across recent runs to catch persistent offenders?
- Standalone page or a panel on the run overview? (overlaps with `ux-revamp`)

---

### ux-revamp
**UX revamp**

**What:** Refresh the evalboard UI ([evalboard/](../evalboard/), Next.js) — clearer run
overview, easier drill-down from suite → skill → task, better surfacing of the metrics
above (pass-rate, cost, turns).

**Why:** The board is the daily touchpoint for reading nightly results; a clearer UX
makes regressions and outliers faster to spot.

**Open questions (needs scoping before it's actionable):**
- What are the top 2–3 jobs-to-be-done on the board today that are painful?
- Incremental polish vs. a rethink of the information architecture?
- Any design input, or engineer's-judgment pass?

---

### efficiency-columns
**Surface already-computed efficiency metrics on the board**

**What:** We already compute per-task efficiency signals and write them into
`run.json`, but the evalboard never shows them. Add columns/badges for
`commands_efficiency`, a `max_turns_exhausted` status badge, total tokens, and a
derived cache-hit-rate (`cache_read / (cache_creation + cache_read)`) — and show
`expected_commands` next to actual so over-budget tasks are obvious at a glance.

**Why:** Cost and efficiency regressions are invisible today. The data is *already
in the artifact* — this is pure surfacing, not new computation, so it's a cheap,
high-leverage win that complements [`expected-turns`](#expected-turns).

**What exists today:**
- `eval_result_to_task_dict()` writes `commands_efficiency`, `max_turns_exhausted`,
  `expected_commands`, and `total_tokens` into each task dict —
  [src/coder_eval/reports_experiment.py:42](../src/coder_eval/reports_experiment.py#L42)
- The grid recomputes/displays only a subset (turns, cost, raw token buckets) —
  [evalboard/app/runs/[id]/task-grid.tsx](../evalboard/app/runs/[id]/task-grid.tsx)
- Turn-budget coloring already exists as a model — [evalboard/lib/turns.ts](../evalboard/lib/turns.ts)

**Rough approach:** widen the evalboard `RawTaskResult` type to read the existing
fields → add sortable columns + a `max_turns_exhausted` badge → reuse the
`turns.ts` color-ratio pattern for an efficiency/cache cell.

**Open questions:**
- Which metrics earn a column vs. a hover/detail-panel stat (avoid grid sprawl)?
- Cache-hit-rate: compute in the loader, or add it to `run.json` once so the
  Slack summary can use it too?

---

### config-fingerprint
**Per-run config fingerprint + regression attribution**

**What:** Stamp each run with a stable fingerprint of the inputs that actually drive
results (resolved task config + agent/model + skills/cli SHAs + framework version),
and add a board view that diffs two runs' fingerprints. When pass-rate moves,
answer "did the model change, the prompt change, or the task config change?"

**Why:** Today a regression between nightlies is a whodunit — you can't separate a
model bump from a prompt rewrite from a config drift. The raw material (config
lineage, component SHAs) is already captured per task; nothing ties it into a
run-level, diffable fingerprint.

**What exists today:**
- Per-task `task_config` + dotted-path config lineage —
  [src/coder_eval/reports_experiment.py:42](../src/coder_eval/reports_experiment.py#L42)
- Component SHAs (coder_eval / skills / cli) already in the run summary and Slack
  footer — the nightly Slack summary
- Single declarative merge resolver produces the resolved config —
  [src/coder_eval/orchestration/config_merge.py](../src/coder_eval/orchestration/config_merge.py)

**Rough approach:** hash the resolved-config + SHA bundle into `run.json` → add a
"compare runs" panel on the evalboard that renders the field-level diff → optionally
flag pass-rate deltas whose fingerprint is *identical* (i.e. pure agent nondeterminism).

**Open questions:**
- Fingerprint at run level, per-skill, or per-task (tasks can differ within a run)?
- What's the canonical diff unit — resolved YAML, or the lineage entries?

---

### judge-ensemble
**Multi-sample judge voting + verdict variance**

**What:** Let `llm_judge` (and optionally `agent_judge`) run N independent samples
per row and aggregate — median/mean score plus a variance/disagreement signal —
instead of trusting a single one-shot verdict. Surface verdict spread so a
"flaky judge" is visible rather than silently noisy.

**Why:** Subjective scoring underpins a large share of tasks, but every judged score
today is a single sample at temperature 0 with no measure of its own reliability. A
judge that would score the same artifact 0.4 / 0.8 / 0.6 looks authoritative. Voting
reduces variance; reporting spread tells us *which rubrics are unreliable*.

**What exists today:**
- Single-sample verdict via forced `submit_verdict` tool call; temperature frozen at
  parse time — [src/coder_eval/criteria/llm_judge.py](../src/coder_eval/criteria/llm_judge.py)
- `JudgeVerdict` (score / rationale / findings) with score clamped to [0,1] —
  [src/coder_eval/models/judge.py](../src/coder_eval/models/judge.py)
- Per-criterion `aggregate()` already exists for suite rollups —
  [src/coder_eval/criteria/base.py:192](../src/coder_eval/criteria/base.py#L192)

**Rough approach:** add `samples: int` (+ aggregation rule) to the judge criteria →
run N calls (concurrently) → fold into one `JudgeCriterionResult` carrying mean +
stddev + raw scores → expose stddev as a suite-thresholdable metric.

**Open questions:**
- Default N and temperature — fixed (e.g. 3 @ T=1.0) or per-criterion?
- Does verdict spread gate the suite, or is it report-only to start?
- Cost ceiling: cap samples on `agent_judge` (expensive) differently from `llm_judge`?

---

### cost-preflight
**Pre-flight budget validation + judge cost estimate**

**What:** Validate run-time budget config *before* a run starts, and estimate judge
token cost before each judge fires. Two concrete fixes: (1) error (or loudly warn)
when `max_usd` is set on a route that can't report cost, instead of silently
skipping the budget; (2) estimate the judge context's token footprint so a giant
file list / dialog can't blow out context unexpectedly.

**Why:** `max_usd` silently no-ops on routes without per-turn cost data — you think
you're capped and you're not, and you only find out at result-finalization. Judges
also burn tokens with zero pre-flight check. Both are cheap guardrails against
surprise cost.

**What exists today:**
- Cost budget skipped with a one-time warning when no per-turn cost is available —
  [src/coder_eval/orchestrator.py:640](../src/coder_eval/orchestrator.py#L640)
- `cost_data_available` recorded *post-hoc* in environment info —
  [src/coder_eval/orchestrator.py:516](../src/coder_eval/orchestrator.py#L516)
- Judge context assembled (files + reference + dialog) with no token estimate —
  [src/coder_eval/evaluation/judge_context.py](../src/coder_eval/evaluation/judge_context.py)

**Rough approach:** add a pre-run validator that cross-checks `run_limits.max_usd`
against the resolved route's cost capability (error or explicit warn) → add a rough
token estimate to `JudgeContextBuilder` and log/cap it before dispatch.

**Open questions:**
- Hard error vs. warn for `max_usd` on a cost-blind route?
- Estimate-only, or auto-trim context to fit a configured ceiling?

---

### failure-mode-rollup
**Failure-mode taxonomy headline**

**What:** Roll the *reasons* tasks failed into a run-level headline — counts of
`max_turns_exhausted`, `TOKEN_BUDGET_EXCEEDED`, `COST_BUDGET_EXCEEDED`, crashes/
timeouts, and the top review tags (e.g. `prompt-gap`) — surfaced in `run.json`, the
Slack summary, and the run page. Turn "62 failures" into "31 prompt-gap, 18
max-turns, 9 crashes, 4 budget."

**Why:** The pass/fail headline says *how many* failed, never *why*. The categories
already exist across the codebase (final-status enums, per-task flags, review tags)
but nothing aggregates them into one actionable breakdown — the input that makes
[`top-offenders`](#top-offenders) and triage useful.

**What exists today:**
- `FinalStatus.TOKEN_BUDGET_EXCEEDED` / `COST_BUDGET_EXCEEDED` and `max_turns_exhausted`
  per task — [src/coder_eval/reports_experiment.py:42](../src/coder_eval/reports_experiment.py#L42)
- Error categorization for crashes/timeouts —
  [src/coder_eval/errors/categorization.py](../src/coder_eval/errors/categorization.py)
- Per-task review tags indexed in `review_index.json` —
  the nightly Slack summary

**Rough approach:** aggregate per-task statuses + flags + tags into a `failure_modes`
block on the run summary → add one section to the Slack summary → render as a
breakdown bar on the run page.

**Open questions:**
- Status enum + flags only, or blend in the (looser) review-tag taxonomy?
- Fixed buckets, or data-driven top-N categories per run?

---

### judge-calibration
**Golden-set judge calibration + drift detection**

**What:** A small, version-controlled set of artifacts with hand-assigned "correct"
scores per rubric, plus a command that runs the judges against it and reports
agreement (e.g. MAE / rank correlation vs. the golden scores). Run it in CI so a
prompt or model change that silently shifts judge behavior trips a gate.

**Why:** Judges have no ground truth to tune against — we can't tell a good rubric
from a lenient one, and a model upgrade can re-baseline every judged score without
anyone noticing. A golden set turns "trust me" into a measurable, regression-tested
property and complements [`judge-ensemble`](#judge-ensemble) (variance) with
*accuracy*.

**What exists today:**
- LLM + agent judges produce structured `JudgeVerdict`s —
  [src/coder_eval/evaluation/judge_verdict.py](../src/coder_eval/evaluation/judge_verdict.py)
- Suite-level aggregation + thresholds already gate on metrics —
  [src/coder_eval/reports.py](../src/coder_eval/reports.py)
- No fixture of graded reference verdicts exists anywhere in the repo.

**Rough approach:** curate ~10–20 graded artifacts per judge rubric under `tests/`
→ add a `coder-eval judge-calibrate` command that scores them and reports
agreement → wire a CI gate on a minimum agreement threshold.

**Open questions:**
- Per-rubric golden sets, or one shared spread of easy/medium/hard cases?
- Agreement metric — MAE, Spearman rank, or pass/fail confusion at the threshold?

---

### result-cache
**Skip-unchanged task result cache**

**What:** Cache completed task results keyed by a fingerprint of everything that
determines the outcome (resolved task config + agent/model + skills/cli SHA +
template/sandbox inputs). On the next run, tasks whose fingerprint is unchanged
*and* deterministic can be reused instead of re-executed — dramatically cutting
nightly wall-clock and cost.

**Why:** Every nightly re-runs every task from scratch even when nothing relevant
changed, burning the 4h budget and real money. A content-addressed cache lets the
nightly spend its budget on what actually changed. (Pairs naturally with
[`config-fingerprint`](#config-fingerprint), which produces the same key.)

**What exists today:**
- Batch runner already supports *resume* — partition prior results, re-run only
  failed/skipped — [src/coder_eval/orchestration/batch.py](../src/coder_eval/orchestration/batch.py)
- No inter-run result cache; runs are fully independent.
- Agent runs are stochastic, so caching must be opt-in / fingerprint-gated.

**Rough approach:** reuse the [`config-fingerprint`](#config-fingerprint) key →
store last-good results in a content-addressed store → on run start, short-circuit
tasks with a cache hit (respecting a `--no-cache` / replicate-count override) →
clearly mark reused vs. fresh in `run.json`.

**Open questions:**
- Does caching even make sense given agent nondeterminism — only for deterministic
  criteria, or never reuse the *agent* run and only cache *grading*?
- Where does the cache live (local dir vs. blob) and how is it invalidated/expired?

---

### smart-judge-truncation
**Smarter judge context truncation**

**What:** Replace the naïve "drop trailing turns when over budget" dialog truncation
with a strategy that preserves high-signal context — keep first + last K turns, or
middle-ellipsis — so the judge never loses the final user question on a long
simulation. There's already a TODO in the code calling for exactly this.

**Why:** On long dialogs, FIFO-dropping the *most recent* turns can discard the
decisive exchange the rubric needs, quietly degrading verdict quality. The fix is
local and well-scoped, and it directly improves judge reliability.

**What exists today:**
- `JudgeContextBuilder` truncation with a standing TODO ("keep first+last K, or
  middle-ellipsis ... the naïve cap is enough for the common case") —
  [src/coder_eval/evaluation/judge_context.py](../src/coder_eval/evaluation/judge_context.py)
- Per-file cap + aggregate dialog cap (`max_dialog_chars`, default 80K), with
  degradation already marked in the context notes.

**Rough approach:** implement a first+last-K (or middle-ellipsis) selector behind
the existing truncation point → record which turns were elided in the degradation
note → keep the naïve cap as a fallback option.

**Open questions:**
- First+last-K vs. middle-ellipsis vs. relevance-scored selection — start simple?
- Same strategy for per-file truncation, or dialog-only for now?

---

### per-criterion-telemetry
**Per-criterion timing + token telemetry**

**What:** Record wall-clock time and (for judges) token/cost spend per criterion, so
we can answer "which criterion is slow/expensive?" Today criteria — especially the
two judge types — are a black box inside the per-task total.

**Why:** When a task is slow or pricey, there's no way to attribute it to a specific
criterion. Judges in particular can dominate cost, and we can't see it. This is the
instrumentation that makes [`long-failing`](#long-failing) and cost triage precise
rather than guesswork.

**What exists today:**
- `CriterionResult` carries score/details but no timing or token attribution —
  [src/coder_eval/models/results.py](../src/coder_eval/models/results.py)
- Judge token usage is captured at the *task* level (proxy delta / SDK), not
  per-criterion — [src/coder_eval/orchestrator.py](../src/coder_eval/orchestrator.py)
- `SuccessChecker` dispatches criteria sequentially —
  [src/coder_eval/evaluation/checker.py](../src/coder_eval/evaluation/checker.py)

**Rough approach:** wrap each criterion check in a timer in `SuccessChecker` →
add `duration_ms` (+ optional `tokens`/`cost_usd` for judges) to `CriterionResult`
→ surface a per-criterion mini-breakdown on the task detail page.

**Open questions:**
- Token attribution for judges — exact (per-call delta) or best-effort?
- Worth running independent criteria concurrently while we're in here, or out of scope?

---

### diff-criterion
**Diff/patch validation criterion**

**What:** A first-class criterion that validates the *change* an agent made — expected
files touched/untouched, hunks present, lines added/removed within bounds — by
diffing the sandbox against its starting state. Built for incremental-edit tasks
where "did it modify the right things, and only those?" matters more than final
file content.

**Why:** Coding agents are increasingly graded on edits, not greenfield files. Today
that's only checkable via brittle `file_contains` substrings or custom `run_command`
shell. A diff-aware criterion captures edit intent directly and discourages
over-broad changes (a real failure mode).

**What exists today:**
- 17 criterion types, none diff-aware; closest is `file_contains` / `file_matches_regex` —
  [src/coder_eval/models/criteria.py](../src/coder_eval/models/criteria.py)
- Sandbox starts from templates/starter files, so a baseline-vs-final diff is
  available — [src/coder_eval/sandbox.py](../src/coder_eval/sandbox.py)
- Plugin registry makes adding a checker mechanical (`@register_criterion`) —
  [src/coder_eval/criteria/__init__.py](../src/coder_eval/criteria/__init__.py)

**Rough approach:** add a `diff_check` model to the criteria union → checker computes
the sandbox-vs-baseline diff and scores against expected changed-files / hunk
patterns / add-remove bounds → fractional score so partial edits get partial credit.

**Open questions:**
- Compare against git baseline, a snapshot of starter files, or a reference patch?
- Score on file-set match, hunk match, or both (weighted)?

---

## How to add an idea

1. Pick a short, stable **ID** (kebab-case) and add a `### <id>` section using the
   template above (What / Why / rough approach / open questions).
2. Score it on Impact and Effort (1–5), compute **Score = Impact + (6 − Effort)**.
3. Insert a row in the **Backlog** table at the position its Score implies — that
   position *is* its rank. Done.
