# Watchlist — executive triage page for the evalboard

**Status:** Design approved (2026-06-02). Implements the `top-offenders` backlog idea
under the name **Watchlist**.

## Purpose

A single standalone page leadership can open in a meeting and immediately see
**what to fix first** — instead of scrolling a full run. It ranks skills and tasks
by where the signal says to look: chronic breakage, recent regressions, instability,
and turn-budget bloat — all over a fixed recent-runs window.

This is an *executive* view. It optimizes for a 3-second read, not analyst depth;
the existing `/trends` page remains the per-task analytical surface.

## Scope

**In:**
- New standalone route `/watchlist`, linked from the header nav (label "Watchlist").
- A "Needs Attention — Top 5" hero with a transparent, explainable composite score.
- Five panels (below the hero) covering the agreed axes.
- A new `lib/watchlist.ts` aggregation layer + unit tests, reusing the existing
  recent-runs data already loaded for `/trends`.
- Matches the evalboard house style exactly (system font, `gray-200` borders,
  `studio-blue` accent, `rounded-full` status pills, indigo skill pill).

**Out (explicitly dropped or deferred):**
- **Ownership / CODEOWNER / "message them"** — dropped entirely. No owner data exists
  and the feature was cut during brainstorming.
- **Cost / "money pit"** — dropped from both the hero composite and the panels.
- **Broken vs flaky-harness split** — removed after review. Labeling `ERROR`/`TIMEOUT`
  (or budget/turn-exhaustion stops) as "our harness" is an unsupportable claim — those
  failures can equally be agent-side (e.g. an infinite loop hitting the timeout). We
  will not attribute blame we can't substantiate, so the panel is gone entirely.
- **Window selector** — fixed at the last 10 runs (consistent with `/trends`).
- **Inline skill→task expand** — v1 uses links (skill rows navigate to a filtered
  run view); inline expansion is a possible follow-up.
- **Week-vs-week dual horizon** — we use a single rolling 10-run window; "regression"
  approximates the week-over-week story via latest-run-vs-prior-runs.

## Window

The last **10 runs** (nightly ≈ 10 days), reusing the same window machinery that
powers `/trends` (`lib/overview.ts::loadRecentRuns` + the `TRENDS_RECENT_RUN_COUNT`
constant — promote to a shared `RECENT_RUN_WINDOW` if cleaner). Every metric below is
computed over this window. "Latest run" = the most recent run in the window.

## Unit of analysis

Mixed, by panel:
- **Skill-level** (skill derived from `tasks/<skill>/...` via existing `deriveSkill`):
  hero, leaderboard, Yee-Yaw, turn-overage.
- **Task-level**: "Never passed" and "Failed in sequence".

This mirrors how the conversation framed each axis and is acknowledged as intentional.

## The hero: Needs Attention — Top 5

Skill-level. The five highest **Attention Scores**, descending. Tie-break: higher
fail-rate, then skill name ascending.

### Attention Score (0–100)

```
score = 50·failRate + 30·regression + 20·turnOverage
```

where each component is a 0–1 fraction:

| Component | Definition | Notes |
|---|---|---|
| `failRate` | `1 − passRate` over the window | `passRate = successRuns / totalRuns` aggregated across the skill's (task × run) outcomes. |
| `regression` | `max(0, prevPassRate − recentPassRate)` | `recentPassRate` = skill pass rate in the **latest run**; `prevPassRate` = skill pass rate over the **prior runs** in the window (runs 2..N). If only one run exists, `regression = 0`. |
| `turnOverage` | `clamp(avgTurnRatio − 1, 0, 1)` | `avgTurnRatio` = mean of `totalTurns / expectedTurns` across the skill's runs **where `expectedTurns` is present and > 0**. If no such data, `turnOverage = 0`. 2× budget ⇒ 1.0. |

The score is intentionally cost-free and self-scaling (no relative-to-worst term, no
maintained anchors) so the number is stable and reproducible run-to-run.

### Rendering (treatment "B" — score-bar breakdown)

Each row: rank · skill (mono) · **stacked bar** · score · one-line reason.

- **Bar length = score** (fills to `score/100` of the track).
- **Segments** = each component's *weighted points*, in order
  `failRate·50` (red) → `regression·30` (orange) → `turnOverage·20` (amber).
  Segment widths therefore sum to the score, so the viewer reads *how bad* (length)
  and *why* (composition) at once.
- **Reason** is generated from the dominant component(s): e.g.
  "Failed all 10 · was 80% last week", "Dropped 70% → 20% overnight",
  "Passing, but ~2× turn budget".
- **Hover** reveals the exact component fractions and raw numbers (passRate,
  recent vs prior, avgTurnRatio). A legend maps the three colors.

The formula string is printed under the hero heading so the bar is never opaque.

## Panels (2-column grid, in this order)

1. **🔴 Never passed** *(task-level)* — tasks that were **never SUCCESS** across the
   window, among tasks that appeared in at least ⌈N/2⌉ runs (so one-off tasks don't
   dominate). Row: task id (link) · skill pill · `0/appeared` red pill. Sort by
   appearances desc.
2. **📉 Skills leaderboard** *(skill-level)* — pass rate, **worst → best**, mini green
   pass bar. Show the worst ~10. Row links to the latest run filtered to that skill.
3. **🧯 Failed in sequence, never fixed** *(task-level)* — tasks on an **active failure
   streak** (latest run failed; count consecutive failures backward from newest). Row:
   task id (link) · streak-length pill (red ≥5, amber otherwise). Sort by streak desc.
   May overlap "Never passed" (a 10/10 failure is both) — acceptable and intended.
4. **🎢 Yee-Yaw — least stable** *(skill-level)* — **volatility** = standard deviation
   of the skill's per-run pass rate over the window, shown as `±X%`. Row: skill · a
   pass/fail **sparkline** of recent runs · `±X%`. Sort by volatility desc, top ~5.
5. **🐌 Turn-overage offenders** *(skill-level)* — skills with `avgTurnRatio > 1`,
   sorted desc. Row: skill · "X turns / Y budget" · `R×` pill, tinted with the existing
   `tintForRatio` thresholds (amber > 1.25, red > 1.5 from `lib/turns.ts`).

**Empty states:** each panel renders a friendly "Nothing here 🎉" message when it has
no offenders, so a clean window reads as good news rather than a broken page.

**Expandable lists:** every list (incl. the hero) shows a default cap, then a native
`<details>` "Show all N" expander when there are more — so offenders tied at the same
level (all 0%, same streak length, same score) are never silently truncated. The
summary calls out how many hidden rows are tied with the last visible one. CSS-only
(no client JS). `buildWatchlist` returns the full ranked lists; the view caps display.

## Architecture & data flow

```
app/watchlist/page.tsx  (server component)
  └─ loadRecentRuns()                         # reuse lib/overview.ts window loader
      └─ buildWatchlist(perRuns): WatchlistData   # NEW lib/watchlist.ts (pure)
          → { topAttention[], neverPassed[], leaderboard[],
              streaks[], volatility[], turnOverage[] }
  └─ <WatchlistView data={…} />               # presentational server component (native title tooltips)
```

- `lib/watchlist.ts` is **pure** (input: `PerRun[]` already typed in `lib/overview.ts`;
  output: a `WatchlistData` object of plain serializable rows). All math lives here and
  is unit-tested in isolation. No I/O, no React.
- Reuse existing helpers: `deriveSkill`, `turnRatio`/`tintForRatio` (`lib/turns.ts`),
  `humanizeTaskId`/`fmtRunTime` (`lib/format.ts`),
  `StatusPill` (`lib/pills.tsx`), skill `ChipButton` variant (`app/runs/[id]/chips.tsx`).
- Skill-level aggregates come from grouping `RunOverviewTask` by `skill` across the
  `PerRun[]`; task-level panels group by `taskId` (the same buckets `lib/trends.ts`
  already builds — factor out a shared bucketing helper if it avoids duplication).

## Linking

- Task rows → `/runs/{runId}/{taskId}` for the most recent run in which the task
  appears (existing URL pattern).
- Skill rows → the latest run filtered to that skill (existing chip/tag filtering),
  e.g. `/runs/latest?tag=<skill>` or `/?tag=<skill>` — match whatever the chips use.

## Styling (house-style fidelity)

- System sans (`-apple-system…`), body `#1f2937`, white background.
- Cards: `bg-white border border-gray-200 rounded-lg`, no heavy shadows.
- Status counts use `StatusPill`'s green-50 / red-50 / amber-50 / gray-50 scheme.
- Skill tags use the indigo pill from `chips.tsx`.
- Accent: `studio-blue` for links/active nav; `uipath-orange` as a thin left rule on
  the page title only (sharp, sparing).
- Hero segments: red / orange / amber so the three drivers stay distinct on a projector.
- Content width `max-w-[1400px]`, `px-8 py-6`, matching `layout.tsx`.

## Testing

`lib/__tests__/watchlist.test.ts`, following `trends.test.ts` patterns, with synthetic
`PerRun[]` fixtures covering:
- Attention Score math incl. segment widths summing to the score.
- Component edge cases: single-run window (regression = 0), missing `expectedTurns`
  (turnOverage = 0), all-pass skill (score 0, excluded from hero).
- Never-passed detection + the ⌈N/2⌉ appearance floor.
- Streak counting (active streak only; streak breaks on a SUCCESS).
- Volatility ordering and the all-stable case (±0%).
- Turn-overage threshold tinting and the `>1` filter.
- Empty window → every panel returns an empty list (page renders empty states).

## Verification

- `pnpm test` (vitest) in `evalboard/` green, including the new suite.
- `pnpm lint` / `pnpm typecheck` (or repo equivalents) clean.
- Manual: `pnpm dev`, open `/watchlist` against a real recent-runs window, confirm
  it visually matches the dashboard and the hero math is correct against a known run.

## Edge cases & decisions

- **Few runs:** never-passed requires ≥⌈N/2⌉ appearances; streaks require a failed
  latest run; metrics degrade gracefully when the window has <10 runs.
- **Division by zero:** `expectedTurns` null/0 → skill skipped from turn-overage and
  contributes 0 to the hero's turn component.
- **Ad-hoc runs:** honor whatever `loadRecentRuns` already does for `adhoc` runs in
  `/trends` (do not invent different handling here).
- **Single-task skills:** per-run pass rate is 0/1; volatility/regression still defined
  but noisier — acceptable for v1.
```
