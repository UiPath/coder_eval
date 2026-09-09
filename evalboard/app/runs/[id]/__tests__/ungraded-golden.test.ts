import { describe, expect, test } from "vitest";
import type { TaskResultSummary } from "@/lib/runs";
import { perTaskGradedCounts, perTaskPassCounts } from "@/lib/status";
import { taskVariantKey } from "@/lib/variants";
import { computeRunMetrics, computeVariantMetrics } from "../run-view";

// One invariant, asserted through every rate surface a `coder-eval execute` run
// touches: an UNGRADED row leaves BOTH sides of the rate.
//
// This is arithmetic over a data shape, not a code pattern, so no lint rule
// catches it — and the first ungraded sweep proved a correct guard can still be
// applied to the wrong denominator: `pct` divided by the graded count while the
// LABEL beside it read `passed / total`, so 8 graded passes next to 2 ungraded
// rows rendered "80%" beside "8 / 12", and a plain 12-task execute run rendered
// a red "0%  0 / 12".

function row(
    taskId: string,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        variantId: null,
        replicateIndex: null,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        expectedSeconds: null,
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

const ungraded = (taskId: string, extra: Partial<TaskResultSummary> = {}) =>
    row(taskId, { status: "NOT_GRADED", weightedScore: null, ...extra });

describe("a fully ungraded run reports 0 of 0, never 0%", () => {
    const tasks = Array.from({ length: 12 }, (_, i) => ungraded(`t${i}`));
    const m = computeRunMetrics(tasks);

    test("every row is bucketed as ungraded, none as failed", () => {
        expect(m.total).toBe(12);
        expect(m.ungraded).toBe(12);
        expect(m.failed).toBe(0);
        expect(m.errored).toBe(0);
        expect(m.passed).toBe(0);
    });

    test("the denominator the tile renders is zero, so the tile reads neutral", () => {
        // `graded`, not `total`. The tile's tone is null when this is 0, which is
        // what stops a clean execute run rendering as a red measured 0%.
        expect(m.graded).toBe(0);
    });

    test("the per-task rollup does not score them as failures", () => {
        // 0 of 0 tasks, not 0 of 12: an all-ungraded task is absent from the
        // rollup entirely rather than appearing as a task that failed.
        expect(m.taskTotal).toBe(0);
        expect(m.taskPassed).toBe(0);
        expect(m.taskFailed).toBe(0);
    });
});

describe("a mixed run divides by the graded rows on both halves of the tile", () => {
    const tasks = [
        ...Array.from({ length: 8 }, (_, i) => row(`p${i}`)),
        row("f0", { status: "FAILURE", weightedScore: 0 }),
        row("f1", { status: "FAILURE", weightedScore: 0 }),
        ungraded("u0"),
        ungraded("u1"),
    ];
    const m = computeRunMetrics(tasks);

    test("pct and its label describe the same sample", () => {
        expect(m.total).toBe(12);
        expect(m.graded).toBe(10);
        expect(m.passed).toBe(8);
        expect(m.pct).toBeCloseTo(80);
        // The pair the tile renders: "80%  8 / 10". Dividing by `total` here is
        // the bug this asserts against — it produced "80%  8 / 12".
        expect(m.passed / m.graded).toBeCloseTo(m.pct / 100);
    });

    test("ungraded rows are not counted as failures", () => {
        expect(m.failed).toBe(2);
        expect(m.failedTotal).toBe(2);
    });
});

describe("repeats: an ungraded replicate leaves both sides of the k/N badge", () => {
    const tasks = [
        row("t", { replicateIndex: 0 }),
        ungraded("t", { replicateIndex: 1 }),
        ungraded("u", { replicateIndex: 0 }),
        ungraded("u", { replicateIndex: 1 }),
    ];

    test("the badge reads 1/1, not 1/2", () => {
        const passes = perTaskPassCounts(tasks);
        const graded = perTaskGradedCounts(tasks);
        const key = taskVariantKey({ taskId: "t", variantId: null });
        expect(passes.get(key)).toBe(1);
        expect(graded.get(key)).toBe(1);
    });

    test("a task whose every replicate was ungraded is absent, not 0/2", () => {
        const graded = perTaskGradedCounts(tasks);
        expect(graded.has(taskVariantKey({ taskId: "u", variantId: null }))).toBe(
            false,
        );
    });
});

describe("variants: each arm's rate excludes its own ungraded rows", () => {
    const tasks = [
        row("t0", { variantId: "a" }),
        ungraded("t1", { variantId: "a" }),
        row("t0", { variantId: "b", status: "FAILURE", weightedScore: 0 }),
        ungraded("t1", { variantId: "b" }),
    ];
    const rows = computeVariantMetrics(tasks);

    test("arm a is 100% of one graded task, not 50% of two", () => {
        const a = rows.find((r) => r.variantId === "a")!.metrics;
        expect(a.graded).toBe(1);
        expect(a.taskTotal).toBe(1);
        expect(a.taskPassed).toBe(1);
    });

    test("arm b is a measured 0% — an ungraded row must not soften a real failure", () => {
        const b = rows.find((r) => r.variantId === "b")!.metrics;
        expect(b.graded).toBe(1);
        expect(b.taskTotal).toBe(1);
        expect(b.taskPassed).toBe(0);
    });
});
