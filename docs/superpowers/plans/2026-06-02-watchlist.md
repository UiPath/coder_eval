# Watchlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone `/watchlist` page on the evalboard that ranks skills/tasks by what to fix first over the last 10 runs — a "Needs Attention" hero with a transparent Attention Score plus six diagnostic panels.

**Architecture:** A pure aggregation module `lib/watchlist.ts` turns the already-loaded `PerRun[]` (from `lib/overview.ts::loadRecentRuns`) into a serializable `WatchlistData` object. A server component `app/watchlist/page.tsx` loads the window and renders a presentational server component `app/watchlist/watchlist-view.tsx`. No new data I/O — it reuses the trends window machinery. Detailed score breakdowns use native `title` tooltips (no client component needed in v1).

**Tech Stack:** Next.js (app router, server components), TypeScript, Tailwind, vitest. Working dir: `evalboard/`. Import alias: `@/lib/...`. Run commands from `evalboard/`.

**Spec:** `docs/superpowers/specs/2026-06-02-watchlist-design.md`

> **Superseded (post-review):** The **"Broken vs flaky-harness" panel was removed** after
> review — we can't credibly attribute `ERROR`/`TIMEOUT`/budget stops to "our harness"
> (they can be agent-side). Ignore every `harnessSplit` / `isHarness` / `HarnessRow`
> reference below (Task 4 and the module API/view snippets); the shipped page has **five
> panels**, not six. See the spec's "Out" section for the rationale.

---

## File Structure

- **Create** `evalboard/lib/watchlist.ts` — pure aggregation: types + per-metric functions + `buildWatchlist`. One responsibility: turn `PerRun[]` into ranked rows. No React, no I/O.
- **Create** `evalboard/lib/__tests__/watchlist.test.ts` — unit tests for every metric.
- **Create** `evalboard/app/watchlist/page.tsx` — server component: load window, call `buildWatchlist`, render view.
- **Create** `evalboard/app/watchlist/watchlist-view.tsx` — presentational server component: hero + six panels.
- **Modify** `evalboard/app/layout.tsx:40-47` — add a "Watchlist" nav link beside "Trends".

### Module API (locked — later tasks must match these names exactly)

```ts
// lib/watchlist.ts
export const FAIL_WEIGHT = 50;
export const REG_WEIGHT = 30;
export const TURN_WEIGHT = 20;

export interface AttentionRow {
    skill: string;
    score: number;                 // 0-100
    failRate: number;              // 0-1
    regression: number;            // 0-1
    turnOverage: number;           // 0-1
    segFail: number;               // FAIL_WEIGHT * failRate
    segReg: number;                // REG_WEIGHT * regression
    segTurn: number;               // TURN_WEIGHT * turnOverage
    passRate: number;              // 0-1, window-wide
    recentPassRate: number;        // 0-1, latest run the skill appears in
    prevPassRate: number;          // 0-1, runs before that
    reason: string;
}
export interface NeverPassedRow { taskId: string; skill: string | null; appeared: number; windowSize: number; latestRunId: string; }
export interface LeaderboardRow { skill: string; passRate: number; outcomes: number; }
export interface StreakRow { taskId: string; skill: string | null; streak: number; latestRunId: string; }
export interface VolatilityRow { skill: string; volatility: number; sparkline: number[]; } // sparkline newest-first, 0-1
export interface HarnessRow { skill: string; failure: number; harness: number; total: number; }
export interface TurnOverageRow { skill: string; avgTurnRatio: number; avgTurns: number; avgExpected: number; }
export interface WatchlistData {
    windowSize: number;
    topAttention: AttentionRow[];
    neverPassed: NeverPassedRow[];
    leaderboard: LeaderboardRow[];
    streaks: StreakRow[];
    volatility: VolatilityRow[];
    harnessSplit: HarnessRow[];
    turnOverage: TurnOverageRow[];
}
export function buildWatchlist(perRun: PerRun[]): WatchlistData;
```

**Key semantic decisions (from the spec):**
- "Outcome" = one (task × run) result. Skill pass rate = SUCCESS outcomes / total outcomes for that skill across the window.
- Skill comes from `RunOverviewTask.skill` (already derived). Tasks with `skill == null` are still counted in task-level panels (never-passed, streaks) but skipped from skill-level panels.
- **Harness split** uses Watchlist's own rule (NOT `statusCategory`, which buckets TIMEOUT as "failed"): `harness` = status ∈ {`ERROR`, `TIMEOUT`}; `failure` = any other non-SUCCESS status.
- `turnRatio(totalTurns, expectedTurns)` from `lib/turns.ts` (returns `null` unless both present and `expectedTurns > 0`).
- Runs are ordered newest-first by `id` (timestamped run ids sort lexically).

---

## Task 1: Module scaffold + leaderboard (first real metric)

**Files:**
- Create: `evalboard/lib/watchlist.ts`
- Create: `evalboard/lib/__tests__/watchlist.test.ts`

- [ ] **Step 1: Write the test file with shared fixtures + the first failing test**

```ts
// evalboard/lib/__tests__/watchlist.test.ts
import { describe, expect, test } from "vitest";
import type { PerRun } from "../overview";
import type { RunOverview, RunOverviewTask } from "../runs";
import { buildWatchlist } from "../watchlist";

function task(overrides: Partial<RunOverviewTask>): RunOverviewTask {
    return {
        taskId: "t1",
        status: "SUCCESS",
        tags: [],
        skill: null,
        totalCostUsd: null,
        durationSeconds: null,
        weightedScore: null,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        hasFinalReply: false,
        ...overrides,
    };
}
function overview(id: string, tasks: RunOverviewTask[]): RunOverview {
    return { id, tasks, totalCostUsd: null, taskDurationSeconds: null, componentShas: [] };
}
function perRun(id: string, t: RunOverviewTask[]): PerRun {
    return { id, overview: overview(id, t), reviewTagCounts: {}, reviewTagsByTask: {} };
}

describe("leaderboard", () => {
    test("pass rate per skill, worst first", () => {
        const data = buildWatchlist([
            perRun("2026-01-02", [
                task({ taskId: "a", skill: "alpha", status: "FAILURE" }),
                task({ taskId: "b", skill: "beta", status: "SUCCESS" }),
            ]),
            perRun("2026-01-01", [
                task({ taskId: "a", skill: "alpha", status: "FAILURE" }),
                task({ taskId: "b", skill: "beta", status: "FAILURE" }),
            ]),
        ]);
        expect(data.leaderboard).toEqual([
            { skill: "alpha", passRate: 0, outcomes: 2 },
            { skill: "beta", passRate: 0.5, outcomes: 2 },
        ]);
    });
});
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: FAIL — `buildWatchlist` is not exported / file missing.

- [ ] **Step 3: Create `lib/watchlist.ts` with types, helpers, leaderboard, and a stubbed `buildWatchlist`**

```ts
// evalboard/lib/watchlist.ts
// Pure aggregation for the Watchlist page. Input: the PerRun[] already loaded
// for the recent-runs window (see lib/overview.ts). Output: ranked, serializable
// rows. No I/O, no React — fully unit-testable.

import type { PerRun } from "./overview";
import type { RunOverviewTask } from "./runs";
import { turnRatio } from "./turns";

export const FAIL_WEIGHT = 50;
export const REG_WEIGHT = 30;
export const TURN_WEIGHT = 20;

export interface AttentionRow {
    skill: string;
    score: number;
    failRate: number;
    regression: number;
    turnOverage: number;
    segFail: number;
    segReg: number;
    segTurn: number;
    passRate: number;
    recentPassRate: number;
    prevPassRate: number;
    reason: string;
}
export interface NeverPassedRow {
    taskId: string;
    skill: string | null;
    appeared: number;
    windowSize: number;
    latestRunId: string;
}
export interface LeaderboardRow { skill: string; passRate: number; outcomes: number; }
export interface StreakRow { taskId: string; skill: string | null; streak: number; latestRunId: string; }
export interface VolatilityRow { skill: string; volatility: number; sparkline: number[]; }
export interface HarnessRow { skill: string; failure: number; harness: number; total: number; }
export interface TurnOverageRow { skill: string; avgTurnRatio: number; avgTurns: number; avgExpected: number; }
export interface WatchlistData {
    windowSize: number;
    topAttention: AttentionRow[];
    neverPassed: NeverPassedRow[];
    leaderboard: LeaderboardRow[];
    streaks: StreakRow[];
    volatility: VolatilityRow[];
    harnessSplit: HarnessRow[];
    turnOverage: TurnOverageRow[];
}

// Runs newest-first, dropping any with a null overview.
interface LoadedRun { id: string; tasks: RunOverviewTask[]; }
function runsNewestFirst(perRun: PerRun[]): LoadedRun[] {
    return [...perRun]
        .filter((r) => r.overview != null)
        .sort((a, b) => b.id.localeCompare(a.id))
        .map((r) => ({ id: r.id, tasks: r.overview!.tasks }));
}

const isPass = (status: string | null) => status === "SUCCESS";

// ---- Leaderboard: pass rate per skill, worst first ----
export function leaderboard(runs: LoadedRun[]): LeaderboardRow[] {
    const total = new Map<string, number>();
    const passed = new Map<string, number>();
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill) continue;
            total.set(t.skill, (total.get(t.skill) ?? 0) + 1);
            if (isPass(t.status)) passed.set(t.skill, (passed.get(t.skill) ?? 0) + 1);
        }
    }
    return [...total.entries()]
        .map(([skill, outcomes]) => ({
            skill,
            outcomes,
            passRate: (passed.get(skill) ?? 0) / outcomes,
        }))
        .sort(
            (a, b) =>
                a.passRate - b.passRate ||
                b.outcomes - a.outcomes ||
                a.skill.localeCompare(b.skill),
        );
}

export function buildWatchlist(perRun: PerRun[]): WatchlistData {
    const runs = runsNewestFirst(perRun);
    return {
        windowSize: runs.length,
        topAttention: [],
        neverPassed: [],
        leaderboard: leaderboard(runs),
        streaks: [],
        volatility: [],
        harnessSplit: [],
        turnOverage: [],
    };
}
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add evalboard/lib/watchlist.ts evalboard/lib/__tests__/watchlist.test.ts
git commit -m "feat(watchlist): aggregation scaffold + skills leaderboard"
```

---

## Task 2: Attention Score (hero)

**Files:**
- Modify: `evalboard/lib/watchlist.ts`
- Test: `evalboard/lib/__tests__/watchlist.test.ts`

- [ ] **Step 1: Add failing tests**

```ts
describe("attention score", () => {
    test("chronic failure dominates and excludes all-pass skills", () => {
        const runs = Array.from({ length: 4 }, (_, i) =>
            perRun(`2026-01-0${4 - i}`, [
                task({ taskId: "a", skill: "broken", status: "FAILURE" }),
                task({ taskId: "b", skill: "fine", status: "SUCCESS" }),
            ]),
        );
        const { topAttention } = buildWatchlist(runs);
        expect(topAttention).toHaveLength(1); // "fine" has score 0, excluded
        const row = topAttention[0];
        expect(row.skill).toBe("broken");
        expect(row.failRate).toBe(1);
        expect(row.regression).toBe(0); // failed every run, no cliff
        expect(row.score).toBe(50); // 50*1 + 30*0 + 20*0
        expect(row.segFail).toBe(50);
        expect(row.reason).toContain("Failed all 4");
    });

    test("regression: passed before, fails now", () => {
        const { topAttention } = buildWatchlist([
            perRun("2026-01-03", [task({ taskId: "a", skill: "reg", status: "FAILURE" })]),
            perRun("2026-01-02", [task({ taskId: "a", skill: "reg", status: "SUCCESS" })]),
            perRun("2026-01-01", [task({ taskId: "a", skill: "reg", status: "SUCCESS" })]),
        ]);
        const row = topAttention[0];
        expect(row.recentPassRate).toBe(0);
        expect(row.prevPassRate).toBe(1);
        expect(row.regression).toBe(1);
        // failRate = 1/3, score = 50*(1/3) + 30*1 = 46.666...
        expect(row.score).toBeCloseTo(46.67, 1);
    });

    test("turn overage contributes for a passing-but-bloated skill", () => {
        const runs = Array.from({ length: 2 }, (_, i) =>
            perRun(`2026-01-0${2 - i}`, [
                task({ taskId: "a", skill: "slow", status: "SUCCESS", totalTurns: 24, expectedTurns: 12 }),
            ]),
        );
        const row = buildWatchlist(runs).topAttention[0];
        expect(row.failRate).toBe(0);
        expect(row.turnOverage).toBe(1); // ratio 2 -> clamp(2-1,0,1)=1
        expect(row.score).toBe(20);
        expect(row.reason).toContain("turn budget");
    });
});
```

- [ ] **Step 2: Run, verify fail**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts -t "attention score"`
Expected: FAIL — `topAttention` is empty.

- [ ] **Step 3: Implement the hero in `lib/watchlist.ts`**

Add these helpers and replace `topAttention: []` in `buildWatchlist` with `topAttention: attention(runs)`:

```ts
const clamp01 = (n: number) => (n < 0 ? 0 : n > 1 ? 1 : n);
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

function pct(n: number): string {
    return `${Math.round(n * 100)}%`;
}

// Per-run pass rate for a skill (SUCCESS share among that skill's tasks in one
// run), newest-first, only for runs where the skill appears.
function skillPassSeq(runs: LoadedRun[], skill: string): number[] {
    const seq: number[] = [];
    for (const run of runs) {
        const ts = run.tasks.filter((t) => t.skill === skill);
        if (ts.length === 0) continue;
        seq.push(ts.filter((t) => isPass(t.status)).length / ts.length);
    }
    return seq;
}

function attentionReason(r: {
    failRate: number;
    regression: number;
    turnOverage: number;
    passRate: number;
    prevPassRate: number;
    recentPassRate: number;
    appeared: number;
}): string {
    const parts: number[] = [
        FAIL_WEIGHT * r.failRate,
        REG_WEIGHT * r.regression,
        TURN_WEIGHT * r.turnOverage,
    ];
    const top = parts.indexOf(Math.max(...parts));
    if (top === 1 && r.regression > 0) {
        return `Dropped ${pct(r.prevPassRate)} → ${pct(r.recentPassRate)} recently`;
    }
    if (top === 2 && r.turnOverage > 0) {
        return "Passing, but well over turn budget";
    }
    if (r.passRate === 0) return `Failed all ${r.appeared} runs`;
    return `${pct(r.failRate)} fail rate`;
}

export function attention(runs: LoadedRun[]): AttentionRow[] {
    const skills = new Set<string>();
    for (const run of runs) for (const t of run.tasks) if (t.skill) skills.add(t.skill);

    const rows: AttentionRow[] = [];
    for (const skill of skills) {
        // outcomes for fail rate + turn ratios
        let outcomes = 0;
        let passes = 0;
        let appeared = 0;
        const ratios: number[] = [];
        for (const run of runs) {
            const ts = run.tasks.filter((t) => t.skill === skill);
            if (ts.length > 0) appeared++;
            for (const t of ts) {
                outcomes++;
                if (isPass(t.status)) passes++;
                const r = turnRatio(t.totalTurns, t.expectedTurns);
                if (r != null) ratios.push(r);
            }
        }
        const passRate = outcomes ? passes / outcomes : 0;
        const failRate = 1 - passRate;

        const seq = skillPassSeq(runs, skill);
        const recentPassRate = seq.length ? seq[0] : passRate;
        const prevPassRate = seq.length > 1 ? mean(seq.slice(1)) : recentPassRate;
        const regression = clamp01(prevPassRate - recentPassRate);

        const turnOverage = ratios.length ? clamp01(mean(ratios) - 1) : 0;

        const segFail = FAIL_WEIGHT * failRate;
        const segReg = REG_WEIGHT * regression;
        const segTurn = TURN_WEIGHT * turnOverage;
        const score = segFail + segReg + segTurn;
        if (score <= 0) continue;

        rows.push({
            skill,
            score,
            failRate,
            regression,
            turnOverage,
            segFail,
            segReg,
            segTurn,
            passRate,
            recentPassRate,
            prevPassRate,
            reason: attentionReason({
                failRate,
                regression,
                turnOverage,
                passRate,
                prevPassRate,
                recentPassRate,
                appeared,
            }),
        });
    }
    return rows
        .sort(
            (a, b) =>
                b.score - a.score ||
                b.failRate - a.failRate ||
                a.skill.localeCompare(b.skill),
        )
        .slice(0, 5);
}
```

- [ ] **Step 4: Run, verify pass**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: PASS (all attention + leaderboard tests).

- [ ] **Step 5: Commit**

```bash
git add evalboard/lib/watchlist.ts evalboard/lib/__tests__/watchlist.test.ts
git commit -m "feat(watchlist): needs-attention score (hero)"
```

---

## Task 3: Never-passed + failed-in-sequence (task-level)

**Files:**
- Modify: `evalboard/lib/watchlist.ts`
- Test: `evalboard/lib/__tests__/watchlist.test.ts`

- [ ] **Step 1: Add failing tests**

```ts
describe("never passed", () => {
    test("only tasks failing every run AND appearing in >= half the window", () => {
        // window = 4 runs; "ghost" appears once (below floor of 2) -> excluded
        const data = buildWatchlist([
            perRun("2026-01-04", [
                task({ taskId: "dead", skill: "s", status: "FAILURE" }),
                task({ taskId: "ghost", skill: "s", status: "FAILURE" }),
            ]),
            perRun("2026-01-03", [task({ taskId: "dead", skill: "s", status: "FAILURE" })]),
            perRun("2026-01-02", [task({ taskId: "dead", skill: "s", status: "ERROR" })]),
            perRun("2026-01-01", [task({ taskId: "alive", skill: "s", status: "SUCCESS" })]),
        ]);
        expect(data.neverPassed).toEqual([
            { taskId: "dead", skill: "s", appeared: 3, windowSize: 4, latestRunId: "2026-01-04" },
        ]);
    });
});

describe("streaks", () => {
    test("active losing streak counts back from newest, stops at a pass", () => {
        const data = buildWatchlist([
            perRun("2026-01-04", [task({ taskId: "x", skill: "s", status: "FAILURE" })]),
            perRun("2026-01-03", [task({ taskId: "x", skill: "s", status: "TIMEOUT" })]),
            perRun("2026-01-02", [task({ taskId: "x", skill: "s", status: "SUCCESS" })]),
            perRun("2026-01-01", [task({ taskId: "x", skill: "s", status: "FAILURE" })]),
        ]);
        expect(data.streaks).toEqual([
            { taskId: "x", skill: "s", streak: 2, latestRunId: "2026-01-04" },
        ]);
    });

    test("a passing latest run means no active streak", () => {
        const data = buildWatchlist([
            perRun("2026-01-02", [task({ taskId: "x", skill: "s", status: "SUCCESS" })]),
            perRun("2026-01-01", [task({ taskId: "x", skill: "s", status: "FAILURE" })]),
        ]);
        expect(data.streaks).toEqual([]);
    });
});
```

- [ ] **Step 2: Run, verify fail**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts -t "never passed"`
Expected: FAIL — `neverPassed` empty.

- [ ] **Step 3: Implement both, wire into `buildWatchlist`**

Replace `neverPassed: []` with `neverPassed: neverPassed(runs)` and `streaks: []` with `streaks: streaks(runs)`. Add:

```ts
// Per-task status sequence newest-first, with the run id each came from.
interface TaskSeqEntry { runId: string; status: string | null; skill: string | null; }
function taskSequences(runs: LoadedRun[]): Map<string, TaskSeqEntry[]> {
    const m = new Map<string, TaskSeqEntry[]>();
    for (const run of runs) {
        for (const t of run.tasks) {
            let seq = m.get(t.taskId);
            if (!seq) {
                seq = [];
                m.set(t.taskId, seq);
            }
            seq.push({ runId: run.id, status: t.status, skill: t.skill });
        }
    }
    return m; // entries are newest-first because runs are newest-first
}

export function neverPassed(runs: LoadedRun[]): NeverPassedRow[] {
    const windowSize = runs.length;
    const floor = Math.ceil(windowSize / 2);
    const rows: NeverPassedRow[] = [];
    for (const [taskId, seq] of taskSequences(runs)) {
        const appeared = seq.length;
        if (appeared < floor) continue;
        if (seq.some((e) => isPass(e.status))) continue;
        rows.push({
            taskId,
            skill: seq[0].skill,
            appeared,
            windowSize,
            latestRunId: seq[0].runId,
        });
    }
    return rows.sort((a, b) => b.appeared - a.appeared || a.taskId.localeCompare(b.taskId));
}

export function streaks(runs: LoadedRun[]): StreakRow[] {
    const rows: StreakRow[] = [];
    for (const [taskId, seq] of taskSequences(runs)) {
        let streak = 0;
        for (const e of seq) {
            if (isPass(e.status)) break;
            streak++;
        }
        if (streak === 0) continue; // latest run passed -> not an active streak
        rows.push({ taskId, skill: seq[0].skill, streak, latestRunId: seq[0].runId });
    }
    return rows.sort((a, b) => b.streak - a.streak || a.taskId.localeCompare(b.taskId));
}
```

- [ ] **Step 4: Run, verify pass**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evalboard/lib/watchlist.ts evalboard/lib/__tests__/watchlist.test.ts
git commit -m "feat(watchlist): never-passed + failure-streak panels"
```

---

## Task 4: Yee-Yaw volatility + harness split + turn-overage (skill-level)

**Files:**
- Modify: `evalboard/lib/watchlist.ts`
- Test: `evalboard/lib/__tests__/watchlist.test.ts`

- [ ] **Step 1: Add failing tests**

```ts
describe("volatility", () => {
    test("alternating pass/fail is more volatile than steady; sparkline newest-first", () => {
        const flip = (id: string, status: string) =>
            perRun(id, [task({ taskId: "f", skill: "flap", status })]);
        const data = buildWatchlist([
            flip("2026-01-04", "FAILURE"),
            flip("2026-01-03", "SUCCESS"),
            flip("2026-01-02", "FAILURE"),
            flip("2026-01-01", "SUCCESS"),
        ]);
        expect(data.volatility[0].skill).toBe("flap");
        expect(data.volatility[0].sparkline).toEqual([0, 1, 0, 1]);
        expect(data.volatility[0].volatility).toBeCloseTo(0.5, 5); // stdev of [0,1,0,1]
    });
});

describe("harness split", () => {
    test("ERROR/TIMEOUT are harness; FAILURE is genuine; SUCCESS ignored", () => {
        const data = buildWatchlist([
            perRun("2026-01-01", [
                task({ taskId: "a", skill: "s", status: "FAILURE" }),
                task({ taskId: "b", skill: "s", status: "ERROR" }),
                task({ taskId: "c", skill: "s", status: "TIMEOUT" }),
                task({ taskId: "d", skill: "s", status: "SUCCESS" }),
            ]),
        ]);
        expect(data.harnessSplit).toEqual([
            { skill: "s", failure: 1, harness: 2, total: 3 },
        ]);
    });
});

describe("turn overage", () => {
    test("only skills averaging over budget, sorted by ratio desc", () => {
        const data = buildWatchlist([
            perRun("2026-01-01", [
                task({ taskId: "a", skill: "slow", status: "SUCCESS", totalTurns: 18, expectedTurns: 12 }),
                task({ taskId: "b", skill: "ok", status: "SUCCESS", totalTurns: 6, expectedTurns: 12 }),
            ]),
        ]);
        expect(data.turnOverage).toEqual([
            { skill: "slow", avgTurnRatio: 1.5, avgTurns: 18, avgExpected: 12 },
        ]);
    });
});
```

- [ ] **Step 2: Run, verify fail**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts -t "volatility"`
Expected: FAIL — `volatility` empty.

- [ ] **Step 3: Implement all three, wire into `buildWatchlist`**

Replace the three remaining `[]` placeholders with `volatility: volatility(runs)`, `harnessSplit: harnessSplit(runs)`, `turnOverage: turnOverage(runs)`. Add:

```ts
function stdev(xs: number[]): number {
    if (xs.length < 2) return 0;
    const m = mean(xs);
    return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
}

export function volatility(runs: LoadedRun[]): VolatilityRow[] {
    const skills = new Set<string>();
    for (const run of runs) for (const t of run.tasks) if (t.skill) skills.add(t.skill);
    const rows: VolatilityRow[] = [];
    for (const skill of skills) {
        const seq = skillPassSeq(runs, skill); // newest-first
        if (seq.length < 2) continue;
        rows.push({ skill, sparkline: seq, volatility: stdev(seq) });
    }
    return rows.sort((a, b) => b.volatility - a.volatility || a.skill.localeCompare(b.skill));
}

const isHarness = (status: string | null) => status === "ERROR" || status === "TIMEOUT";

export function harnessSplit(runs: LoadedRun[]): HarnessRow[] {
    const fail = new Map<string, number>();
    const harness = new Map<string, number>();
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill || isPass(t.status)) continue;
            if (isHarness(t.status)) harness.set(t.skill, (harness.get(t.skill) ?? 0) + 1);
            else fail.set(t.skill, (fail.get(t.skill) ?? 0) + 1);
        }
    }
    const skills = new Set([...fail.keys(), ...harness.keys()]);
    return [...skills]
        .map((skill) => {
            const failure = fail.get(skill) ?? 0;
            const h = harness.get(skill) ?? 0;
            return { skill, failure, harness: h, total: failure + h };
        })
        .sort((a, b) => b.total - a.total || a.skill.localeCompare(b.skill));
}

export function turnOverage(runs: LoadedRun[]): TurnOverageRow[] {
    const ratios = new Map<string, number[]>();
    const turns = new Map<string, number[]>();
    const expected = new Map<string, number[]>();
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill) continue;
            const r = turnRatio(t.totalTurns, t.expectedTurns);
            if (r == null) continue;
            (ratios.get(t.skill) ?? ratios.set(t.skill, []).get(t.skill)!).push(r);
            (turns.get(t.skill) ?? turns.set(t.skill, []).get(t.skill)!).push(t.totalTurns!);
            (expected.get(t.skill) ?? expected.set(t.skill, []).get(t.skill)!).push(t.expectedTurns!);
        }
    }
    const rows: TurnOverageRow[] = [];
    for (const [skill, rs] of ratios) {
        const avgTurnRatio = mean(rs);
        if (avgTurnRatio <= 1) continue;
        rows.push({
            skill,
            avgTurnRatio,
            avgTurns: mean(turns.get(skill)!),
            avgExpected: mean(expected.get(skill)!),
        });
    }
    return rows.sort((a, b) => b.avgTurnRatio - a.avgTurnRatio || a.skill.localeCompare(b.skill));
}
```

- [ ] **Step 4: Run, verify pass**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evalboard/lib/watchlist.ts evalboard/lib/__tests__/watchlist.test.ts
git commit -m "feat(watchlist): volatility, harness split, turn-overage panels"
```

---

## Task 5: Empty-window safety test

**Files:**
- Test: `evalboard/lib/__tests__/watchlist.test.ts`

- [ ] **Step 1: Add the test**

```ts
describe("empty window", () => {
    test("no runs -> every panel empty, no throw", () => {
        const data = buildWatchlist([]);
        expect(data).toEqual({
            windowSize: 0,
            topAttention: [],
            neverPassed: [],
            leaderboard: [],
            streaks: [],
            volatility: [],
            harnessSplit: [],
            turnOverage: [],
        });
    });

    test("runs with null overview are skipped", () => {
        const data = buildWatchlist([
            { id: "2026-01-01", overview: null, reviewTagCounts: {}, reviewTagsByTask: {} },
        ]);
        expect(data.windowSize).toBe(0);
    });
});
```

- [ ] **Step 2: Run, verify pass (no code change needed — guards already exist)**

Run: `cd evalboard && pnpm vitest run lib/__tests__/watchlist.test.ts`
Expected: PASS. If any panel throws on empty input, fix the guard in that function, then re-run.

- [ ] **Step 3: Commit**

```bash
git add evalboard/lib/__tests__/watchlist.test.ts
git commit -m "test(watchlist): empty-window safety"
```

---

## Task 6: Page route + view component

**Files:**
- Create: `evalboard/app/watchlist/page.tsx`
- Create: `evalboard/app/watchlist/watchlist-view.tsx`

- [ ] **Step 1: Create the server page**

```tsx
// evalboard/app/watchlist/page.tsx
import { loadRecentRuns } from "@/lib/overview";
import { TRENDS_RECENT_RUN_COUNT } from "@/lib/trends";
import { buildWatchlist } from "@/lib/watchlist";
import { fmtRunTime } from "@/lib/format";
import { WatchlistView } from "./watchlist-view";

export const dynamic = "force-dynamic";

export default async function WatchlistPage() {
    const perRun = await loadRecentRuns(TRENDS_RECENT_RUN_COUNT);
    const data = buildWatchlist(perRun);
    const ids = perRun.map((r) => r.id).sort();
    const newest = ids.length ? fmtRunTime(ids[ids.length - 1]) : null;
    return <WatchlistView data={data} newest={newest} />;
}
```

- [ ] **Step 2: Create the view component**

```tsx
// evalboard/app/watchlist/watchlist-view.tsx
// Presentational server component. Renders the Needs-Attention hero + six
// panels in the evalboard house style. Detailed score numbers ride on native
// `title` tooltips (hover) — no client interactivity needed in v1.
import Link from "next/link";
import { humanizeTaskId } from "@/lib/format";
import {
    FAIL_WEIGHT,
    REG_WEIGHT,
    TURN_WEIGHT,
    type AttentionRow,
    type WatchlistData,
} from "@/lib/watchlist";

const pct = (n: number) => `${Math.round(n * 100)}%`;

function skillHref(skill: string) {
    return `/?tag=${encodeURIComponent(skill)}`;
}
function taskHref(runId: string, taskId: string) {
    return `/runs/${runId}/${taskId}`;
}

function Empty({ children }: { children: React.ReactNode }) {
    return <p className="text-sm text-gray-400 py-2">{children} 🎉</p>;
}

function Panel({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
    return (
        <section className="bg-white border border-gray-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
            <p className="text-[10px] uppercase tracking-wide text-gray-400 mt-0.5 mb-3">{sub}</p>
            {children}
        </section>
    );
}

function HeroRow({ row, rank, max }: { row: AttentionRow; rank: number; max: number }) {
    const w = (pts: number) => `${max > 0 ? (pts / max) * 100 : 0}%`;
    const tip =
        `pass ${pct(row.passRate)} · recent ${pct(row.recentPassRate)} vs prior ${pct(row.prevPassRate)}\n` +
        `fail ${FAIL_WEIGHT}·${row.failRate.toFixed(2)}  reg ${REG_WEIGHT}·${row.regression.toFixed(2)}  turn ${TURN_WEIGHT}·${row.turnOverage.toFixed(2)}`;
    return (
        <div className="flex items-center gap-3.5 py-2.5 border-b border-gray-100 last:border-b-0" title={tip}>
            <span className={`w-5 text-center font-extrabold text-base ${rank === 1 ? "text-red-700" : "text-gray-400"}`}>{rank}</span>
            <Link href={skillHref(row.skill)} className="font-mono text-xs font-semibold text-gray-900 min-w-[190px] hover:text-studio-blue">
                {row.skill}
            </Link>
            <div className="flex-1 min-w-[180px] h-[18px] rounded-[9px] bg-gray-100 overflow-hidden flex">
                <span className="h-full bg-red-500" style={{ width: w(row.segFail) }} />
                <span className="h-full bg-orange-500" style={{ width: w(row.segReg) }} />
                <span className="h-full bg-amber-400" style={{ width: w(row.segTurn) }} />
            </div>
            <span className="w-8 text-right font-extrabold text-base text-gray-900">{Math.round(row.score)}</span>
            <span className="text-[11.5px] text-gray-600 min-w-[200px]">{row.reason}</span>
        </div>
    );
}

export function WatchlistView({ data, newest }: { data: WatchlistData; newest: string | null }) {
    const max = Math.max(100, ...data.topAttention.map((r) => r.score));
    return (
        <div>
            <div className="flex items-baseline gap-3">
                <h1 className="text-[22px] font-bold text-gray-900 border-l-[3px] border-uipath-orange pl-2.5">Watchlist</h1>
                <span className="ml-auto text-[11px] text-gray-600 bg-gray-100 px-3 py-1 rounded-full font-semibold">
                    last {data.windowSize} runs{newest ? ` · through ${newest}` : ""}
                </span>
            </div>
            <p className="text-gray-500 text-[13px] mt-1.5 mb-6">
                What leadership should be watching — ranked by where the signal says to look first.
            </p>

            {/* HERO */}
            <section className="bg-white border border-gray-200 rounded-lg p-5 mb-6">
                <div className="flex items-center gap-2">
                    <h2 className="text-base font-bold text-gray-900">Needs Attention</h2>
                    <span className="text-[10px] font-bold uppercase tracking-wide text-uipath-orange bg-[#fff3ee] border border-[#ffd9cc] px-2 py-0.5 rounded-full">Top 5</span>
                </div>
                <p className="text-gray-500 text-[11px] mt-1 mb-4">
                    Attention Score = {FAIL_WEIGHT}·fail-rate + {REG_WEIGHT}·regression + {TURN_WEIGHT}·turn-overage · bar length = score, segments = what drove it
                </p>
                {data.topAttention.length === 0 ? (
                    <Empty>Nothing needs attention</Empty>
                ) : (
                    data.topAttention.map((row, i) => <HeroRow key={row.skill} row={row} rank={i + 1} max={max} />)
                )}
                <div className="mt-3.5 flex gap-4 text-[11px] text-gray-600">
                    <span><i className="inline-block w-2.5 h-2.5 rounded-sm bg-red-500 mr-1.5 align-middle" />fail-rate</span>
                    <span><i className="inline-block w-2.5 h-2.5 rounded-sm bg-orange-500 mr-1.5 align-middle" />regression</span>
                    <span><i className="inline-block w-2.5 h-2.5 rounded-sm bg-amber-400 mr-1.5 align-middle" />turn-overage</span>
                    <span className="ml-auto text-gray-400">hover a row for the breakdown</span>
                </div>
            </section>

            {/* PANELS */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <Panel title="🔴 Never passed" sub="Chronically broken · failed every run in window">
                    {data.neverPassed.length === 0 ? <Empty>None</Empty> : data.neverPassed.slice(0, 8).map((r) => (
                        <div key={r.taskId} className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs">
                            <Link href={taskHref(r.latestRunId, r.taskId)} className="font-mono text-[11px] text-gray-700 hover:text-studio-blue">{humanizeTaskId(r.taskId)}</Link>
                            {r.skill && <span className="bg-indigo-50 text-indigo-700 rounded-full px-2 py-0.5 text-[10px] font-semibold">{r.skill}</span>}
                            <span className="ml-auto font-bold text-red-700">0/{r.appeared}</span>
                        </div>
                    ))}
                </Panel>

                <Panel title="📉 Skills leaderboard" sub="Pass rate · worst → best">
                    {data.leaderboard.length === 0 ? <Empty>No skills</Empty> : data.leaderboard.slice(0, 10).map((r) => (
                        <div key={r.skill} className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs">
                            <Link href={skillHref(r.skill)} className="font-mono text-[11px] text-gray-700 min-w-[120px] hover:text-studio-blue">{r.skill}</Link>
                            <span className="flex-1 min-w-[70px] h-[7px] rounded bg-red-50 overflow-hidden">
                                <i className="block h-full bg-green-600" style={{ width: pct(r.passRate) }} />
                            </span>
                            <span className={`ml-auto font-bold ${r.passRate < 0.5 ? "text-red-700" : r.passRate < 0.9 ? "text-amber-700" : "text-green-700"}`}>{pct(r.passRate)}</span>
                        </div>
                    ))}
                </Panel>

                <Panel title="🧯 Failed in sequence, never fixed" sub="Active losing streak from the latest run">
                    {data.streaks.length === 0 ? <Empty>No active streaks</Empty> : data.streaks.slice(0, 8).map((r) => (
                        <div key={r.taskId} className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs">
                            <Link href={taskHref(r.latestRunId, r.taskId)} className="font-mono text-[11px] text-gray-700 hover:text-studio-blue">{humanizeTaskId(r.taskId)}</Link>
                            <span className={`ml-auto font-semibold rounded-full px-2.5 py-0.5 text-[11px] border ${r.streak >= 5 ? "bg-red-50 text-red-700 border-red-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}>{r.streak} in a row</span>
                        </div>
                    ))}
                </Panel>

                <Panel title="🎢 Yee-Yaw — least stable" sub="Biggest run-to-run swing (flaky)">
                    {data.volatility.length === 0 ? <Empty>All stable</Empty> : data.volatility.slice(0, 6).map((r) => (
                        <div key={r.skill} className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs">
                            <Link href={skillHref(r.skill)} className="font-mono text-[11px] text-gray-700 min-w-[105px] hover:text-studio-blue">{r.skill}</Link>
                            <span className="flex gap-0.5 items-end h-[18px]">
                                {[...r.sparkline].reverse().map((p, i) => (
                                    <b key={i} className={`w-[5px] rounded-sm block ${p >= 0.5 ? "bg-green-600" : "bg-red-500"}`} style={{ height: p >= 0.5 ? "16px" : "6px" }} />
                                ))}
                            </span>
                            <span className="ml-auto font-bold text-amber-700">±{Math.round(r.volatility * 100)}%</span>
                        </div>
                    ))}
                </Panel>

                <Panel title="🔧 Broken vs flaky-harness" sub="Is it the skill, or our pipeline?">
                    {data.harnessSplit.length === 0 ? <Empty>No failures</Empty> : data.harnessSplit.slice(0, 8).map((r) => (
                        <div key={r.skill} className="flex items-center gap-2 py-1.5 text-xs" title={`${r.failure} FAILURE · ${r.harness} ERROR/TIMEOUT`}>
                            <span className="font-mono text-[11px] text-gray-700 min-w-[105px]">{r.skill}</span>
                            <span className="flex-1 min-w-[120px] h-[14px] rounded-[7px] bg-gray-100 overflow-hidden flex">
                                <span className="h-full bg-red-500" style={{ width: `${(r.failure / r.total) * 100}%` }} />
                                <span className="h-full bg-purple-500" style={{ width: `${(r.harness / r.total) * 100}%` }} />
                            </span>
                        </div>
                    ))}
                    <div className="text-[10.5px] text-gray-500 mt-2">
                        <i className="inline-block w-2.5 h-2.5 rounded-sm bg-red-500 mr-1.5 align-middle" />genuine FAILURE
                        <i className="inline-block w-2.5 h-2.5 rounded-sm bg-purple-500 ml-3 mr-1.5 align-middle" />ERROR / TIMEOUT (our harness)
                    </div>
                </Panel>

                <Panel title="🐌 Turn-overage offenders" sub="Passing, but grinding past budget">
                    {data.turnOverage.length === 0 ? <Empty>All within budget</Empty> : data.turnOverage.slice(0, 8).map((r) => (
                        <div key={r.skill} className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs">
                            <span className="font-mono text-[11px] text-gray-700 min-w-[105px]">{r.skill}</span>
                            <span className="flex-1 text-[11px] text-gray-500">{Math.round(r.avgTurns)} turns / {Math.round(r.avgExpected)} budget</span>
                            <span className={`ml-auto font-semibold rounded-full px-2.5 py-0.5 text-[11px] border ${r.avgTurnRatio > 1.5 ? "bg-red-50 text-red-700 border-red-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}>{r.avgTurnRatio.toFixed(1)}×</span>
                        </div>
                    ))}
                </Panel>
            </div>

            <p className="text-gray-400 text-[11px] mt-5">Task rows link to their task detail · skill rows filter the dashboard to that skill.</p>
        </div>
    );
}
```

- [ ] **Step 3: Typecheck the new files**

Run: `cd evalboard && pnpm typecheck`
Expected: clean. Fix any type error before committing (common: unused import, `React` namespace — if `React.ReactNode` errors, change to `import type { ReactNode } from "react"` and use `ReactNode`).

- [ ] **Step 4: Commit**

```bash
git add evalboard/app/watchlist/page.tsx evalboard/app/watchlist/watchlist-view.tsx
git commit -m "feat(watchlist): /watchlist page + hero/panels view"
```

---

## Task 7: Nav link

**Files:**
- Modify: `evalboard/app/layout.tsx:40-47`

- [ ] **Step 1: Add the Watchlist link beside Trends**

Replace the nav `<div>` block (currently containing only the Trends link) with:

```tsx
                    <div className="flex items-center gap-4 text-sm shrink-0">
                        <a
                            href="/watchlist"
                            className="text-gray-700 hover:text-studio-blue"
                        >
                            Watchlist
                        </a>
                        <a
                            href="/trends"
                            className="text-gray-700 hover:text-studio-blue"
                        >
                            Trends
                        </a>
                    </div>
```

- [ ] **Step 2: Typecheck**

Run: `cd evalboard && pnpm typecheck`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add evalboard/app/layout.tsx
git commit -m "feat(watchlist): add Watchlist nav link"
```

---

## Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full verify (typecheck + tests + production build)**

Run: `cd evalboard && pnpm verify`
Expected: `tsc --noEmit` clean, all vitest pass (including `watchlist.test.ts`), `next build` succeeds.

- [ ] **Step 2: Manual smoke test**

Run: `cd evalboard && pnpm dev`, open `http://localhost:3030/watchlist`.
Verify: page loads against real recent runs; header shows "Watchlist" nav; hero bars render with three colors; six panels render (or show empty states on a clean window); task links go to `/runs/<id>/<task>`; visual style matches the rest of the dashboard. Spot-check one hero row's reason against the underlying run data.

- [ ] **Step 3: Confirm no regression on other pages**

Open `/` and `/trends`; confirm both still render and the new nav link sits beside Trends.

---

## Self-Review

**Spec coverage:**
- Standalone `/watchlist` + nav → Tasks 6, 7. ✓
- Last-10-runs window via `loadRecentRuns(TRENDS_RECENT_RUN_COUNT)` → Task 6. ✓
- Attention Score 50/30/20 with segments summing to score + reasons → Task 2. ✓
- Six panels (never-passed w/ ≥half floor, leaderboard worst→best, active streaks, volatility sparkline, harness split, turn-overage>1) → Tasks 1,3,4. ✓
- Empty states + empty-window safety → Tasks 5, 6. ✓
- House style (system font, gray-200 cards, studio-blue, indigo skill pill, uipath-orange rule) → Task 6. ✓
- Pure, tested `lib/watchlist.ts` → Tasks 1-5. ✓
- Linking (`/runs/<id>/<task>`, skill → `/?tag=`) → Task 6. ✓

**Deviations from spec (intentional simplifications):**
- No client component — v1 uses native `title` tooltips for the breakdown instead of a JS hover panel. KISS; matches "client component for tooltips only" being optional.
- Harness split intentionally diverges from `statusCategory` (TIMEOUT → harness, not failed) per the exec framing; documented in the module header decision list.

**Placeholder scan:** none — every code step is complete.

**Type consistency:** `buildWatchlist`, `attention`, `leaderboard`, `neverPassed`, `streaks`, `volatility`, `harnessSplit`, `turnOverage`, and all row interfaces match between `lib/watchlist.ts` (Tasks 1-4) and `watchlist-view.tsx` (Task 6). Weights `FAIL_WEIGHT/REG_WEIGHT/TURN_WEIGHT` exported from the module and reused in the view.
```
