import { describe, expect, test, vi } from "vitest";
import type { PerRun } from "../overview";
import type { RunOverview, RunOverviewTask } from "../runs";

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
    return {
        id,
        overview: overview(id, t),
        reviewTagCounts: {},
        reviewTagsByTask: {},
    };
}

// Stub the overview module so historyForTaskInner / aggregate can run
// without touching blob storage. vi.mock is hoisted so the stub lands
// before the SUT imports.
vi.mock("../overview", async () => {
    const actual =
        await vi.importActual<typeof import("../overview")>("../overview");
    return { ...actual, loadRecentRuns: vi.fn() };
});

const { aggregate, historyForTaskInner } = await import("../trends");
const { loadRecentRuns } = await import("../overview");

describe("aggregate — avgTotalTurns", () => {
    test("averages SUCCESS rows only and excludes failures", () => {
        const { trends } = aggregate([
            perRun("r3", [task({ status: "SUCCESS", totalTurns: 6 })]),
            perRun("r2", [task({ status: "SUCCESS", totalTurns: 8 })]),
            perRun("r1", [task({ status: "FAILED", totalTurns: 100 })]),
        ]);
        expect(trends).toHaveLength(1);
        expect(trends[0].avgTotalTurns).toBe(7);
    });

    test("returns null when no SUCCESS rows have total_turns", () => {
        const { trends } = aggregate([
            perRun("r1", [task({ status: "FAILED", totalTurns: 100 })]),
        ]);
        expect(trends[0].avgTotalTurns).toBeNull();
    });

    test("legacy rows with null totalTurns contribute nothing", () => {
        const { trends } = aggregate([
            perRun("r1", [task({ status: "SUCCESS", totalTurns: null })]),
            perRun("r2", [task({ status: "SUCCESS", totalTurns: 4 })]),
        ]);
        expect(trends[0].avgTotalTurns).toBe(4);
    });
});

describe("aggregate — mature-skipped rows", () => {
    test("count as a pass but stay out of cost/duration/turns averages", () => {
        const { trends } = aggregate([
            perRun("r2", [
                task({
                    status: "SUCCESS",
                    totalCostUsd: 1.0,
                    durationSeconds: 100,
                    totalTurns: 6,
                }),
            ]),
            // Skipped this run: carried-forward pass with 0 cost / 0 duration
            // and no turns. It must lift neither the averages toward zero...
            perRun("r1", [
                task({
                    status: "SUCCESS",
                    matureSkipped: true,
                    totalCostUsd: 0,
                    durationSeconds: 0,
                    totalTurns: null,
                }),
            ]),
        ]);
        expect(trends).toHaveLength(1);
        // ...nor drop the pass rate: the skip still counts as a SUCCESS run.
        expect(trends[0].totalRuns).toBe(2);
        expect(trends[0].successRuns).toBe(2);
        expect(trends[0].passRate).toBe(1);
        // Averages reflect only the executed run, not the carried-forward zero.
        expect(trends[0].avgCostUsd).toBe(1.0);
        expect(trends[0].avgDurationSeconds).toBe(100);
        expect(trends[0].avgTotalTurns).toBe(6);
        // The skip is tallied and flagged per-run so the view can surface it.
        expect(trends[0].matureSkips).toBe(1);
        expect(
            trends[0].recentStatuses.map((s) => [s.runId, s.matureSkipped]),
        ).toEqual([
            ["r2", false],
            ["r1", true],
        ]);
    });
});

describe("aggregate — run axis", () => {
    test("empty input yields an empty axis and no trends", () => {
        expect(aggregate([])).toEqual({ runIds: [], trends: [] });
    });

    test("runIds is newest-first", () => {
        const { runIds } = aggregate([
            perRun("r1", [task({})]),
            perRun("r3", [task({})]),
            perRun("r2", [task({})]),
        ]);
        expect(runIds).toEqual(["r3", "r2", "r1"]);
    });

    test("runIds skips runs whose overview failed to load", () => {
        const failed: PerRun = {
            id: "r2",
            overview: null,
            reviewTagCounts: {},
            reviewTagsByTask: {},
        };
        const { runIds } = aggregate([perRun("r1", [task({})]), failed]);
        expect(runIds).toEqual(["r1"]);
    });

    test("runIds skips runs with zero tasks", () => {
        const { runIds } = aggregate([
            perRun("r1", [task({})]),
            perRun("r2", []), // loaded but no tasks — contributes nothing
        ]);
        expect(runIds).toEqual(["r1"]);
    });

    test("a task absent from a run gets no status entry for that run", () => {
        // Mirrors a renamed/retired test: present in the two older runs,
        // missing from the newest. The strip data must expose the gap (by
        // omitting r3) rather than padding it.
        const { runIds, trends } = aggregate([
            perRun("r1", [task({ taskId: "old" })]),
            perRun("r2", [task({ taskId: "old" })]),
            perRun("r3", [task({ taskId: "new" })]),
        ]);
        expect(runIds).toEqual(["r3", "r2", "r1"]);
        const old = trends.find((t) => t.taskId === "old");
        expect(old?.totalRuns).toBe(2);
        expect(old?.recentStatuses.map((s) => s.runId)).toEqual(["r2", "r1"]);
    });
});

describe("historyForTaskInner", () => {
    test("propagates totalTurns and expectedTurns onto each entry", async () => {
        vi.mocked(loadRecentRuns).mockResolvedValueOnce([
            perRun("r1", [
                task({
                    taskId: "t1",
                    status: "SUCCESS",
                    totalTurns: 12,
                    expectedSeconds: 100,
                }),
            ]),
            perRun("r2", [
                task({
                    taskId: "t1",
                    status: "FAILED",
                    totalTurns: 3,
                    expectedSeconds: 100,
                }),
            ]),
        ]);
        const entries = await historyForTaskInner("t1", 10);
        expect(entries).toHaveLength(2);
        // Sorted newest-first by runId.
        expect(entries[0].runId).toBe("r2");
        expect(entries[0].totalTurns).toBe(3);
        expect(entries[0].expectedSeconds).toBe(100);
        expect(entries[1].totalTurns).toBe(12);
        expect(entries[1].expectedSeconds).toBe(100);
    });

    test("legacy rows fall through as null", async () => {
        vi.mocked(loadRecentRuns).mockResolvedValueOnce([
            perRun("r1", [task({ taskId: "t1", status: "SUCCESS" })]),
        ]);
        const entries = await historyForTaskInner("t1", 10);
        expect(entries[0].totalTurns).toBeNull();
        expect(entries[0].expectedSeconds).toBeNull();
    });

    test("flags mature-skipped entries; normal rows are false", async () => {
        vi.mocked(loadRecentRuns).mockResolvedValueOnce([
            perRun("r2", [task({ taskId: "t1", status: "SUCCESS" })]),
            perRun("r1", [
                task({ taskId: "t1", status: "SUCCESS", matureSkipped: true }),
            ]),
        ]);
        const entries = await historyForTaskInner("t1", 10);
        const byRun = Object.fromEntries(
            entries.map((e) => [e.runId, e.matureSkipped]),
        );
        expect(byRun.r1).toBe(true);
        expect(byRun.r2).toBe(false);
    });
});
