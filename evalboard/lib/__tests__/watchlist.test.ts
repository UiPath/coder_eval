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
        expectedSeconds: null,
        visibleTurns: null,
        hasFinalReply: false,
        ...overrides,
    };
}
function overview(id: string, tasks: RunOverviewTask[]): RunOverview {
    return {
        id,
        tasks,
        totalCostUsd: null,
        taskDurationSeconds: null,
        componentShas: [],
    };
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
        expect(row.tasks).toBe(1); // distinct tasks, not outcomes (1 task x 4 runs)
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

    test("hero excludes skills below the appearance floor", () => {
        // window = 4 runs (floor = 2). "oneoff" fails its single appearance
        // (would score 50) but is too sparse to rank; "chronic" appears in all
        // 4 and fails 2 -> only chronic belongs in the hero.
        const { topAttention } = buildWatchlist([
            perRun("2026-01-04", [
                task({ taskId: "c", skill: "chronic", status: "FAILURE" }),
                task({ taskId: "o", skill: "oneoff", status: "FAILURE" }),
            ]),
            perRun("2026-01-03", [task({ taskId: "c", skill: "chronic", status: "FAILURE" })]),
            perRun("2026-01-02", [task({ taskId: "c", skill: "chronic", status: "SUCCESS" })]),
            perRun("2026-01-01", [task({ taskId: "c", skill: "chronic", status: "SUCCESS" })]),
        ]);
        expect(topAttention.map((r) => r.skill)).toEqual(["chronic"]);
    });

    test("time overage contributes for a passing-but-slow skill", () => {
        const runs = Array.from({ length: 2 }, (_, i) =>
            perRun(`2026-01-0${2 - i}`, [
                task({
                    taskId: "a",
                    skill: "slow",
                    status: "SUCCESS",
                    durationSeconds: 240,
                    expectedSeconds: 120,
                }),
            ]),
        );
        const row = buildWatchlist(runs).topAttention[0];
        expect(row.failRate).toBe(0);
        expect(row.timeOverage).toBe(1); // ratio 2 -> clamp(2-1,0,1)=1
        expect(row.score).toBe(20);
        expect(row.reason).toContain("expected time");
    });
});

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
            {
                taskId: "dead",
                skill: "s",
                appeared: 3,
                windowSize: 4,
                latestRunId: "2026-01-04",
            },
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

describe("turn overage", () => {
    test("only skills averaging over budget, sorted by ratio desc", () => {
        const data = buildWatchlist([
            perRun("2026-01-01", [
                task({
                    taskId: "a",
                    skill: "slow",
                    status: "SUCCESS",
                    durationSeconds: 180,
                    expectedSeconds: 120,
                }),
                task({
                    taskId: "b",
                    skill: "ok",
                    status: "SUCCESS",
                    durationSeconds: 60,
                    expectedSeconds: 120,
                }),
            ]),
        ]);
        expect(data.timeOverage).toEqual([
            {
                skill: "slow",
                avgTimeRatio: 1.5,
                avgSeconds: 180,
                avgExpectedSeconds: 120,
            },
        ]);
    });
});

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
            timeOverage: [],
        });
    });

    test("runs with null overview are skipped", () => {
        const data = buildWatchlist([
            { id: "2026-01-01", overview: null, reviewTagCounts: {}, reviewTagsByTask: {} },
        ]);
        expect(data.windowSize).toBe(0);
    });
});
