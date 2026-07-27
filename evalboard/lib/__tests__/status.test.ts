import { describe, expect, test } from "vitest";
import { collapseReplicates } from "../status";
import type { TaskResultSummary } from "../runs";

function row(
    taskId: string,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        replicateIndex: 0,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands: 1,
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

describe("collapseReplicates", () => {
    test("averages the numeric columns across a task's replicates", () => {
        const [collapsed] = collapseReplicates([
            row("t", { replicateIndex: 0, totalCostUsd: 0.1, durationSeconds: 10, weightedScore: 0.9, actualCommands: 4 }),
            row("t", { replicateIndex: 1, totalCostUsd: 0.2, durationSeconds: 20, weightedScore: 0.6, actualCommands: 8 }),
            row("t", { replicateIndex: 2, totalCostUsd: 0.3, durationSeconds: 30, weightedScore: 0.3, actualCommands: 6 }),
        ]);
        // Cost is the MEAN of the three repeats — not the first run's 0.1.
        expect(collapsed.totalCostUsd).toBeCloseTo(0.2, 10);
        expect(collapsed.durationSeconds).toBeCloseTo(20, 10);
        expect(collapsed.weightedScore).toBeCloseTo(0.6, 10);
        expect(collapsed.actualCommands).toBeCloseTo(6, 10);
    });

    test("keeps categorical fields from the representative (passing replicate wins, then lowest index)", () => {
        const [collapsed] = collapseReplicates([
            row("t", { replicateIndex: 0, status: "FAILURE" }),
            row("t", { replicateIndex: 1, status: "SUCCESS" }),
        ]);
        // Representative is the passing replicate (index 1): status + detail link.
        expect(collapsed.status).toBe("SUCCESS");
        expect(collapsed.replicateIndex).toBe(1);
    });

    test("single replicate passes through unchanged (repeats disabled)", () => {
        const only = row("t", { replicateIndex: 0, totalCostUsd: 0.42, actualCommands: 3 });
        const [collapsed] = collapseReplicates([only]);
        expect(collapsed.totalCostUsd).toBe(0.42);
        expect(collapsed.actualCommands).toBe(3);
        expect(collapsed.status).toBe("SUCCESS");
    });

    test("averages over non-null values; all-null stays null", () => {
        const [collapsed] = collapseReplicates([
            row("t", { replicateIndex: 0, totalCostUsd: 0.1, outputTokens: null }),
            row("t", { replicateIndex: 1, totalCostUsd: null, outputTokens: null }),
        ]);
        // 0.1 averaged over the single non-null value; all-null column → null.
        expect(collapsed.totalCostUsd).toBeCloseTo(0.1, 10);
        expect(collapsed.outputTokens).toBeNull();
    });

    test("one row per task, preserving first-seen order", () => {
        const out = collapseReplicates([
            row("b", { replicateIndex: 0 }),
            row("a", { replicateIndex: 0 }),
            row("b", { replicateIndex: 1 }),
        ]);
        expect(out.map((r) => r.taskId)).toEqual(["b", "a"]);
    });
});
