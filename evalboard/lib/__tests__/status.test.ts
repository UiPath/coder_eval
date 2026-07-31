import { describe, expect, test } from "vitest";
import { collapseReplicates, perTaskPassCounts, taskGroupKey } from "../status";
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
        variant: null,
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

    test("keeps a row PER MODEL — variants sharing a taskId are not merged", () => {
        // A multi-model (A/B) run: three variants ran the SAME task. They share
        // the taskId but are distinct models, so they must stay three rows with
        // their OWN metrics — never averaged into one.
        const out = collapseReplicates([
            row("t", { variant: "kimi-k3", model: "moonshotai/kimi-k3", totalCostUsd: 0.1 }),
            row("t", { variant: "glm-5-2", model: "z-ai/glm-5.2", totalCostUsd: 0.2 }),
            row("t", { variant: "deepseek-v4-pro", model: "deepseek/deepseek-v4-pro", totalCostUsd: 0.3 }),
        ]);
        expect(out).toHaveLength(3);
        expect(out.map((r) => r.variant)).toEqual([
            "kimi-k3",
            "glm-5-2",
            "deepseek-v4-pro",
        ]);
        // Each row keeps its own cost — no cross-model averaging.
        expect(out.map((r) => r.totalCostUsd)).toEqual([0.1, 0.2, 0.3]);
    });

    test("still averages replicates WITHIN one variant of a multi-model run", () => {
        const out = collapseReplicates([
            row("t", { variant: "a", replicateIndex: 0, totalCostUsd: 0.1 }),
            row("t", { variant: "a", replicateIndex: 1, totalCostUsd: 0.3 }),
            row("t", { variant: "b", replicateIndex: 0, totalCostUsd: 1.0 }),
        ]);
        expect(out).toHaveLength(2);
        const a = out.find((r) => r.variant === "a")!;
        const b = out.find((r) => r.variant === "b")!;
        expect(a.totalCostUsd).toBeCloseTo(0.2, 10); // mean of a's two replicates
        expect(b.totalCostUsd).toBeCloseTo(1.0, 10);
    });
});

describe("taskGroupKey", () => {
    test("a null variant collapses to the same key as an explicit 'default'", () => {
        expect(taskGroupKey({ taskId: "t", variant: null })).toBe(
            taskGroupKey({ taskId: "t", variant: "default" }),
        );
    });

    test("different variants of the same task yield different keys", () => {
        expect(taskGroupKey({ taskId: "t", variant: "kimi-k3" })).not.toBe(
            taskGroupKey({ taskId: "t", variant: "glm-5-2" }),
        );
    });

    test("the separator cannot be forged from task-id/variant collisions", () => {
        // "ab"+"c" must not equal "a"+"bc" — the control-char separator can't
        // appear in an id, so no two distinct (task, variant) pairs collide.
        expect(taskGroupKey({ taskId: "ab", variant: "c" })).not.toBe(
            taskGroupKey({ taskId: "a", variant: "bc" }),
        );
    });
});

describe("perTaskPassCounts", () => {
    test("counts each variant of a shared task separately", () => {
        const m = perTaskPassCounts([
            row("t", { variant: "a", status: "SUCCESS" }),
            row("t", { variant: "b", status: "FAILURE" }),
        ]);
        expect(m.size).toBe(2);
        expect(m.get(taskGroupKey({ taskId: "t", variant: "a" }))).toBe(1);
        expect(m.get(taskGroupKey({ taskId: "t", variant: "b" }))).toBe(0);
    });
});
