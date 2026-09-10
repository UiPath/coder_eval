// Pure aggregation for the Watchlist page. Input: the PerRun[] already loaded
// for the recent-runs window (see lib/overview.ts). Output: ranked, serializable
// rows. No I/O, no React — fully unit-testable.
//
// Decisions worth knowing:
//   - "outcome" = one (task x run) result. Skill pass rate = SUCCESS outcomes /
//     total outcomes for that skill across the window.
//   - Skill comes from RunOverviewTask.skill (already derived). Tasks with a
//     null skill still feed task-level panels but are skipped from skill-level
//     ones.

import type { PerRun } from "./overview";
import type { RunOverviewTask } from "./runs";
import { isGraded, isPassStatus } from "./status";
import { timeRatio } from "./timing";
import { turnRatio } from "./turns";

export const FAIL_WEIGHT = 50;
export const REG_WEIGHT = 30;
export const TURN_WEIGHT = 20;

export interface AttentionRow {
    skill: string;
    /** Distinct tasks behind the score — small n means a noisy signal. */
    tasks: number;
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
export interface LeaderboardRow {
    skill: string;
    passRate: number;
    outcomes: number;
}
export interface StreakRow {
    taskId: string;
    skill: string | null;
    streak: number;
    latestRunId: string;
}
export interface VolatilityRow {
    skill: string;
    volatility: number;
    sparkline: number[];
}
export interface TurnOverageRow {
    skill: string;
    avgTurnRatio: number;
    avgTurns: number;
    avgExpected: number;
}
export interface TimeOverageRow {
    skill: string;
    avgTimeRatio: number;
    avgSeconds: number;
    avgExpectedSeconds: number;
}
export interface WatchlistData {
    windowSize: number;
    topAttention: AttentionRow[];
    neverPassed: NeverPassedRow[];
    leaderboard: LeaderboardRow[];
    streaks: StreakRow[];
    volatility: VolatilityRow[];
    turnOverage: TurnOverageRow[];
    // Wall-clock sibling of turnOverage, reported alongside it while both
    // efficiency signals run. Deliberately NOT folded into the attention score:
    // the 50/30/20 fail/regression/turn split stays put so the ranking does not
    // move while the derived expected-time line is still being watched. When the
    // turn budget goes, attention() switches its third term to this ratio and
    // this becomes the only overage list.
    timeOverage: TimeOverageRow[];
}

// Runs newest-first, dropping any with a null overview.
interface LoadedRun {
    id: string;
    tasks: RunOverviewTask[];
}
function runsNewestFirst(perRun: PerRun[]): LoadedRun[] {
    return [...perRun]
        .filter((r) => r.overview != null)
        .sort((a, b) => b.id.localeCompare(a.id))
        .map((r) => ({ id: r.id, tasks: r.overview!.tasks }));
}

// Delegates rather than restating `=== "SUCCESS"`: this file already had to
// learn about the ungraded bucket, and a second private spelling of "passed"
// is how one surface ends up disagreeing with the rest.
const isPass = (status: string | null) => isPassStatus(status);

const clamp01 = (n: number) => (n < 0 ? 0 : n > 1 ? 1 : n);
const mean = (xs: number[]) =>
    xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
const pct = (n: number) => `${Math.round(n * 100)}%`;

function stdev(xs: number[]): number {
    if (xs.length < 2) return 0;
    const m = mean(xs);
    return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
}

// Per-run pass rate for a skill (SUCCESS share among that skill's tasks in one
// run), newest-first, only for runs where the skill appears.
function skillPassSeq(runs: LoadedRun[], skill: string): number[] {
    const seq: number[] = [];
    for (const run of runs) {
        const ts = run.tasks.filter(
            (t) => t.skill === skill && isGraded(t.status),
        );
        if (ts.length === 0) continue;
        seq.push(ts.filter((t) => isPass(t.status)).length / ts.length);
    }
    return seq;
}

function allSkills(runs: LoadedRun[]): Set<string> {
    const skills = new Set<string>();
    for (const run of runs)
        for (const t of run.tasks) if (t.skill) skills.add(t.skill);
    return skills;
}

// ---- Leaderboard: pass rate per skill, worst first ----
export function leaderboard(runs: LoadedRun[]): LeaderboardRow[] {
    const total = new Map<string, number>();
    const passed = new Map<string, number>();
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill) continue;
            // Ungraded rows leave both sides of the rate — see isGraded.
            if (!isGraded(t.status)) continue;
            total.set(t.skill, (total.get(t.skill) ?? 0) + 1);
            if (isPass(t.status))
                passed.set(t.skill, (passed.get(t.skill) ?? 0) + 1);
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

// ---- Attention hero ----
function attentionReason(r: {
    failRate: number;
    regression: number;
    turnOverage: number;
    passRate: number;
    prevPassRate: number;
    recentPassRate: number;
    appeared: number;
}): string {
    const parts = [
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
    if (r.passRate === 0) {
        return r.appeared === 1
            ? "Failed its only run"
            : `Failed all ${r.appeared} runs`;
    }
    return `${pct(r.failRate)} fail rate`;
}

export function attention(runs: LoadedRun[]): AttentionRow[] {
    // Same appearance floor as neverPassed: a skill seen in fewer than half the
    // window's runs is too sparse to rank in an exec-triage hero, where a single
    // one-off failure (score 50) would otherwise outrank a chronic offender.
    const floor = Math.ceil(runs.length / 2);
    const rows: AttentionRow[] = [];
    for (const skill of allSkills(runs)) {
        let outcomes = 0;
        let passes = 0;
        let appeared = 0;
        const ratios: number[] = [];
        const taskIds = new Set<string>();
        for (const run of runs) {
            const ts = run.tasks.filter((t) => t.skill === skill);
            if (ts.length > 0) appeared++;
            for (const t of ts) {
                // Ungraded rows leave both sides of the rate — see isGraded.
                if (!isGraded(t.status)) continue;
                outcomes++;
                taskIds.add(t.taskId);
                if (isPass(t.status)) passes++;
                const r = turnRatio(t.totalTurns, t.expectedTurns);
                if (r != null) ratios.push(r);
            }
        }
        if (appeared < floor) continue;
        // Nothing measured ⇒ nothing to say. Without this, a skill whose rows
        // were all ungraded fell into `outcomes ? … : 0` below and scored
        // passRate 0 / failRate 1 — the maximum FAIL_WEIGHT — so a
        // `coder-eval execute` night would put its most-run skill at the TOP of
        // an exec-triage hero, described as chronically failing. The same
        // "never measured" vs "measured and scored zero" confusion CE049 exists
        // to prevent on the Python side.
        if (outcomes === 0) continue;

        const passRate = outcomes ? passes / outcomes : 0;
        const failRate = 1 - passRate;

        const seq = skillPassSeq(runs, skill);
        const recentPassRate = seq.length ? seq[0] : passRate;
        const prevPassRate =
            seq.length > 1 ? mean(seq.slice(1)) : recentPassRate;
        const regression = clamp01(prevPassRate - recentPassRate);

        const turnOverage = ratios.length ? clamp01(mean(ratios) - 1) : 0;

        const segFail = FAIL_WEIGHT * failRate;
        const segReg = REG_WEIGHT * regression;
        const segTurn = TURN_WEIGHT * turnOverage;
        const score = segFail + segReg + segTurn;
        if (score <= 0) continue;

        rows.push({
            skill,
            tasks: taskIds.size,
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
    // Full ranked list (worst first). The view shows the top few and offers an
    // expander, so an exec can reveal every offender tied at the same level
    // rather than have equally-bad skills silently truncated.
    return rows.sort(
        (a, b) =>
            b.score - a.score ||
            b.failRate - a.failRate ||
            a.skill.localeCompare(b.skill),
    );
}

// ---- Task-level sequences ----
interface TaskSeqEntry {
    runId: string;
    status: string | null;
    skill: string | null;
}
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
    return m; // entries newest-first because runs are newest-first
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
    return rows.sort(
        (a, b) => b.appeared - a.appeared || a.taskId.localeCompare(b.taskId),
    );
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
        rows.push({
            taskId,
            skill: seq[0].skill,
            streak,
            latestRunId: seq[0].runId,
        });
    }
    return rows.sort(
        (a, b) => b.streak - a.streak || a.taskId.localeCompare(b.taskId),
    );
}

// ---- Skill-level diagnostics ----
export function volatility(runs: LoadedRun[]): VolatilityRow[] {
    const rows: VolatilityRow[] = [];
    for (const skill of allSkills(runs)) {
        const seq = skillPassSeq(runs, skill); // newest-first
        if (seq.length < 2) continue;
        rows.push({ skill, sparkline: seq, volatility: stdev(seq) });
    }
    return rows.sort(
        (a, b) => b.volatility - a.volatility || a.skill.localeCompare(b.skill),
    );
}

export function turnOverage(runs: LoadedRun[]): TurnOverageRow[] {
    const ratios = new Map<string, number[]>();
    const turns = new Map<string, number[]>();
    const expected = new Map<string, number[]>();
    const push = (m: Map<string, number[]>, k: string, v: number) => {
        const arr = m.get(k);
        if (arr) arr.push(v);
        else m.set(k, [v]);
    };
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill) continue;
            const r = turnRatio(t.totalTurns, t.expectedTurns);
            if (r == null) continue;
            push(ratios, t.skill, r);
            push(turns, t.skill, t.totalTurns!);
            push(expected, t.skill, t.expectedTurns!);
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
    return rows.sort(
        (a, b) =>
            b.avgTurnRatio - a.avgTurnRatio || a.skill.localeCompare(b.skill),
    );
}

// Skills whose passing tasks run past the wall clock they are expected to need.
// Same shape as turnOverage, over `expected_seconds` (derived per task per
// harness by the eval runner) instead of a hand-written turn budget.
export function timeOverage(runs: LoadedRun[]): TimeOverageRow[] {
    const ratios = new Map<string, number[]>();
    const seconds = new Map<string, number[]>();
    const expected = new Map<string, number[]>();
    const push = (m: Map<string, number[]>, k: string, v: number) => {
        const arr = m.get(k);
        if (arr) arr.push(v);
        else m.set(k, [v]);
    };
    for (const run of runs) {
        for (const t of run.tasks) {
            if (!t.skill) continue;
            const r = timeRatio(t.durationSeconds, t.expectedSeconds);
            if (r == null) continue;
            push(ratios, t.skill, r);
            push(seconds, t.skill, t.durationSeconds!);
            push(expected, t.skill, t.expectedSeconds!);
        }
    }
    const rows: TimeOverageRow[] = [];
    for (const [skill, rs] of ratios) {
        const avgTimeRatio = mean(rs);
        if (avgTimeRatio <= 1) continue;
        rows.push({
            skill,
            avgTimeRatio,
            avgSeconds: mean(seconds.get(skill)!),
            avgExpectedSeconds: mean(expected.get(skill)!),
        });
    }
    return rows.sort(
        (a, b) =>
            b.avgTimeRatio - a.avgTimeRatio || a.skill.localeCompare(b.skill),
    );
}

export function buildWatchlist(perRun: PerRun[]): WatchlistData {
    const runs = runsNewestFirst(perRun);
    return {
        windowSize: runs.length,
        topAttention: attention(runs),
        neverPassed: neverPassed(runs),
        leaderboard: leaderboard(runs),
        streaks: streaks(runs),
        volatility: volatility(runs),
        turnOverage: turnOverage(runs),
        timeOverage: timeOverage(runs),
    };
}
