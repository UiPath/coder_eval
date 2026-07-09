import { describe, expect, test } from "vitest";
import type { TaskResultSummary } from "@/lib/runs";
import { computeRunMetrics } from "../run-view";

function row(
    taskId: string,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        replicateIndex: null,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        hasFinalReply: false,
        inputTokens: null,
        outputTokens: null,
        cacheCreationTokens: null,
        cacheReadTokens: null,
        model: null,
        tags: [],
        skill: null,
        matureSkipped: false,
        ...extra,
    };
}

describe("computeRunMetrics — mature-skipped exclusion", () => {
    test("mature rows count as passes but are excluded from cost/duration totals and percentiles", () => {
        const m = computeRunMetrics([
            row("ran-a", { totalCostUsd: 0.1, durationSeconds: 1.0 }),
            row("ran-b", { totalCostUsd: 0.3, durationSeconds: 3.0 }),
            // Carried-forward mature row: 0 cost / 0 duration, never ran.
            row("mature", {
                totalCostUsd: 0,
                durationSeconds: 0,
                matureSkipped: true,
            }),
        ]);

        // Counted toward the run: all three are passes.
        expect(m.total).toBe(3);
        expect(m.passed).toBe(3);
        expect(m.failedTotal).toBe(0);
        expect(m.pct).toBe(100);

        // Totals reflect only the two tasks that actually ran (0.1 + 0.3, 1 + 3).
        expect(m.cost).toBeCloseTo(0.4, 10);
        expect(m.duration).toBeCloseTo(4.0, 10);

        // Percentiles are taken over [0.1, 0.3] / [1, 3] only. If the mature 0s
        // leaked in, the p50 would be dragged to 0.1 / 1.0 (median of three).
        expect(m.costP50).toBeCloseTo(0.2, 10);
        expect(m.costP90).toBeCloseTo(0.28, 10);
        expect(m.durationP50).toBeCloseTo(2.0, 10);
    });

    test("an all-mature run reports passes but null cost/duration (renders as —)", () => {
        const m = computeRunMetrics([
            row("m1", { totalCostUsd: 0, durationSeconds: 0, matureSkipped: true }),
            row("m2", { totalCostUsd: 0, durationSeconds: 0, matureSkipped: true }),
        ]);

        expect(m.total).toBe(2);
        expect(m.passed).toBe(2);
        expect(m.pct).toBe(100);
        expect(m.cost).toBeNull();
        expect(m.costP50).toBeNull();
        expect(m.costP90).toBeNull();
        expect(m.duration).toBeNull();
        expect(m.durationP50).toBeNull();
    });
});

describe("computeRunMetrics — per-task pass rate across replicates", () => {
    test("a task counts as passed if ANY replicate passed; per-replicate rate is separate", () => {
        const m = computeRunMetrics([
            // task A: 1 of 3 replicates passed → task passes
            row("A", { replicateIndex: 0, status: "SUCCESS" }),
            row("A", { replicateIndex: 1, status: "FAILURE" }),
            row("A", { replicateIndex: 2, status: "FAILURE" }),
            // task B: 0 of 2 passed → task fails
            row("B", { replicateIndex: 0, status: "FAILURE" }),
            row("B", { replicateIndex: 1, status: "ERROR" }),
        ]);

        // Per-replicate view (main's existing semantics): 1 pass of 5 rows.
        expect(m.total).toBe(5);
        expect(m.passed).toBe(1);

        // Per-task view (new): 2 distinct tasks, 1 with ≥1 passing replicate,
        // 1 with none → taskFailed is per-task, not the 4 failed replicate rows.
        expect(m.taskTotal).toBe(2);
        expect(m.taskPassed).toBe(1);
        expect(m.taskFailed).toBe(1);
        expect(m.failedTotal).toBe(4); // per-replicate, for the sub-line
    });

    test("single-shot run: per-task metrics mirror per-replicate (no repeats)", () => {
        const m = computeRunMetrics([
            row("A", { status: "SUCCESS" }),
            row("B", { status: "FAILURE" }),
        ]);
        expect(m.total).toBe(2);
        expect(m.passed).toBe(1);
        // taskTotal === total signals "no repeats" → UI shows the plain rate.
        expect(m.taskTotal).toBe(m.total);
        expect(m.taskPassed).toBe(m.passed);
        expect(m.taskFailed).toBe(m.failedTotal);
    });
});
