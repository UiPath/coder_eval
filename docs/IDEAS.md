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
| [`expected-turns`](#expected-turns) | Top-level Expected Turns score | 4 | 2 | 8 | 💡 | — |
| [`long-failing`](#long-failing) | PR a fix for long-running failed tests | 4 | 3 | 7 | 💡 | — |
| [`top-offenders`](#top-offenders) | Top Offenders page | 3 | 3 | 6 | 💡 | — |
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
render on the evalboard + add one line to [dashboard/scripts/ci/slack_summary.py](../dashboard/scripts/ci/slack_summary.py).

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

## How to add an idea

1. Pick a short, stable **ID** (kebab-case) and add a `### <id>` section using the
   template above (What / Why / rough approach / open questions).
2. Score it on Impact and Effort (1–5), compute **Score = Impact + (6 − Effort)**.
3. Insert a row in the **Backlog** table at the position its Score implies — that
   position *is* its rank. Done.
