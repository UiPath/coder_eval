import { describe, expect, test } from "vitest";
import { turnBudgetRateForTasks } from "../overview";
import type { RunOverviewTask } from "../runs";

function task(overrides: Partial<RunOverviewTask>): RunOverviewTask {
    return {
        taskId: "t",
        status: "SUCCESS",
        tags: [],
        skill: null,
        totalCostUsd: null,
        durationSeconds: null,
        weightedScore: null,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        visibleTurns: null,
        hasFinalReply: false,
        ...overrides,
    };
}

describe("turnBudgetRateForTasks", () => {
    test("null when no task carries a budget", () => {
        expect(
            turnBudgetRateForTasks([
                task({ visibleTurns: 5 }),
                task({ status: "FAILURE" }),
            ]),
        ).toBeNull();
    });

    test("100% when every budgeted SUCCESS task is within budget", () => {
        expect(
            turnBudgetRateForTasks([
                task({ expectedTurns: 10, visibleTurns: 7 }),
                task({ expectedTurns: 6, visibleTurns: 9 }), // exactly 1.5×
            ]),
        ).toBe(100);
    });

    test("computes the within-budget share", () => {
        // 1 within, 1 over → 50% of 2 eligible.
        expect(
            turnBudgetRateForTasks([
                task({ expectedTurns: 10, visibleTurns: 12 }), // within (<=15)
                task({ expectedTurns: 10, visibleTurns: 16 }), // over (>15)
            ]),
        ).toBe(50);
    });

    test("excludes tasks without a budget from the denominator", () => {
        // Only the budgeted task counts; the budget-less one is ignored.
        expect(
            turnBudgetRateForTasks([
                task({ expectedTurns: 10, visibleTurns: 12 }),
                task({ visibleTurns: 99 }),
            ]),
        ).toBe(100);
    });

    test("excludes failed/crashed tasks even when cheap", () => {
        // A crashed task with a low visible count must NOT count as within budget.
        const rate = turnBudgetRateForTasks([
            task({ expectedTurns: 10, visibleTurns: 20 }), // SUCCESS, over → fail
            task({ status: "FAILURE", expectedTurns: 10, visibleTurns: 2 }), // excluded
            task({ status: "ERROR", expectedTurns: 10, visibleTurns: 1 }), // excluded
        ]);
        // Only the one SUCCESS task is eligible, and it's over budget → 0%.
        expect(rate).toBe(0);
    });

    test("only reflects the tasks passed in (scoping contract)", () => {
        // getOverview hands this function the already tag/q-scoped list, so the
        // rate is whatever that subset implies — here a single within-budget task.
        expect(
            turnBudgetRateForTasks([task({ expectedTurns: 8, visibleTurns: 8 })]),
        ).toBe(100);
    });
});
