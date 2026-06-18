import { describe, expect, test, vi } from "vitest";
import {
    buildAdhocRows,
    collectPipelineRuns,
    turnBudgetRateForTasks,
    type PerRun,
} from "../overview";
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
    test("null when no task in scope carries a budget", () => {
        // No task carries an expected_turns budget, so none is eligible and the
        // final eligible>0 check returns null (the chart shows a gap).
        expect(turnBudgetRateForTasks([task({ visibleTurns: 5 })])).toBeNull();
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

    test("failed/crashed tasks count as over budget even when cheap", () => {
        // A crashed task with a low visible count must NOT count as within budget;
        // it's treated as having exhausted its turn budget (infinite turns).
        const rate = turnBudgetRateForTasks([
            task({ expectedTurns: 10, visibleTurns: 20 }), // SUCCESS, over → fail
            task({ status: "FAILURE", expectedTurns: 10, visibleTurns: 2 }), // over
            task({ status: "ERROR", expectedTurns: 10, visibleTurns: 1 }), // over
        ]);
        // All 3 eligible, none within budget → 0%.
        expect(rate).toBe(0);
    });

    test("budget-less failures are excluded from the denominator", () => {
        // Eligibility is symmetric: a failure with no expected_turns budget is
        // excluded just like a budget-less success, so it cannot drag the rate
        // down. Only the budgeted within-budget SUCCESS counts → 100%.
        const rate = turnBudgetRateForTasks([
            task({ expectedTurns: 10, visibleTurns: 7 }), // budgeted, within
            task({ status: "FAILURE" }), // no budget → excluded
        ]);
        expect(rate).toBe(100);
    });

    test("budgeted SUCCESS with no visible-turn count is excluded", () => {
        // A budgeted SUCCESS task we can't judge (visibleTurns == null) is
        // dropped from the denominator rather than counted as over budget, so
        // it neither helps nor hurts the rate.
        const rate = turnBudgetRateForTasks([
            task({ expectedTurns: 10, visibleTurns: 7 }), // budgeted, within
            task({ expectedTurns: 10, visibleTurns: null }), // no turn data → excluded
        ]);
        // If the null-turns task were counted as over, this would be 50%.
        expect(rate).toBe(100);
    });

    test("null when no task in scope carries a budget, even with failures", () => {
        // A run that never opted into turn budgeting reports nothing rather than
        // a failure-driven 0% — the chart shows a gap, not a misleading point.
        expect(
            turnBudgetRateForTasks([
                task({ status: "FAILURE" }),
                task({ status: "ERROR", visibleTurns: 3 }),
                task({ visibleTurns: 5 }), // budget-less SUCCESS
            ]),
        ).toBeNull();
    });

    test("a budgeted failure counts as over budget (0%)", () => {
        // Once a budget exists in scope, a failed task drags the rate down.
        expect(
            turnBudgetRateForTasks([
                task({ status: "FAILURE", expectedTurns: 10, visibleTurns: 2 }),
            ]),
        ).toBe(0);
    });

    test("only reflects the tasks passed in (scoping contract)", () => {
        // getOverview hands this function the already tag/q-scoped list, so the
        // rate is whatever that subset implies — here a single within-budget task.
        expect(
            turnBudgetRateForTasks([task({ expectedTurns: 8, visibleTurns: 8 })]),
        ).toBe(100);
    });
});

describe("collectPipelineRuns", () => {
    // A usable run: pipeline-cadence with a non-empty overview.
    function run(id: string, adhoc = false): PerRun {
        return {
            id,
            overview: {
                id,
                tasks: [task({ taskId: "t" })],
                totalCostUsd: null,
                taskDurationSeconds: null,
                componentShas: [],
            },
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc,
            title: null,
        };
    }

    // A run whose run.json couldn't be read — loadPerRunForId downgrades
    // transient blob failures (and missing run.json) to this shape.
    function brokenRun(id: string): PerRun {
        return { ...run(id), overview: null };
    }

    test("single fetch round when everything is usable", async () => {
        const load = vi.fn(async (id: string) => run(id));
        const out = await collectPipelineRuns(["r5", "r4", "r3", "r2"], 3, load);
        expect(out.map((r) => r.id)).toEqual(["r5", "r4", "r3"]);
        expect(load).toHaveBeenCalledTimes(3);
    });

    test("adhoc runs don't consume window slots", async () => {
        // r3 is adhoc: the window must extend to r1 instead of coming back
        // one short (the pre-fix behavior sliced before filtering).
        const adhoc = new Set(["r3"]);
        const load = vi.fn(async (id: string) => run(id, adhoc.has(id)));
        const out = await collectPipelineRuns(
            ["r5", "r4", "r3", "r2", "r1"],
            4,
            load,
        );
        expect(out.map((r) => r.id)).toEqual(["r5", "r4", "r2", "r1"]);
        expect(load).toHaveBeenCalledTimes(5);
    });

    test("unreadable runs don't consume window slots", async () => {
        // r4's run.json failed to load (overview null): it must be backfilled
        // just like an adhoc run, not silently shrink the window/axis.
        const broken = new Set(["r4"]);
        const load = vi.fn(async (id: string) =>
            broken.has(id) ? brokenRun(id) : run(id),
        );
        const out = await collectPipelineRuns(
            ["r5", "r4", "r3", "r2", "r1"],
            4,
            load,
        );
        expect(out.map((r) => r.id)).toEqual(["r5", "r3", "r2", "r1"]);
    });

    test("never returns more than limit even when the probe overshoots", async () => {
        // One adhoc run leaves a deficit of 1; the MIN_PROBE_BATCH floor
        // loads several candidates at once — the result must still be the
        // newest `limit` usable runs.
        const adhoc = new Set(["r8"]);
        const ids = ["r9", "r8", "r7", "r6", "r5", "r4", "r3", "r2", "r1"];
        const load = vi.fn(async (id: string) => run(id, adhoc.has(id)));
        const out = await collectPipelineRuns(ids, 2, load);
        expect(out.map((r) => r.id)).toEqual(["r9", "r7"]);
    });

    test("stops without warning when candidates are exhausted", async () => {
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const load = vi.fn(async (id: string) => run(id, true));
        const out = await collectPipelineRuns(["r2", "r1"], 5, load);
        expect(out).toEqual([]);
        expect(load).toHaveBeenCalledTimes(2);
        // Exhaustion is the legitimate "fewer runs exist" case — no warning.
        expect(warn).not.toHaveBeenCalled();
        warn.mockRestore();
    });

    test("scan cap bounds the probe on a pathological unusable stretch", async () => {
        // 40 adhoc candidates, limit 2 → probe at most 2 × RECENT_SCAN_FACTOR
        // ids instead of walking the whole list, and warn-log the truncation
        // (candidates remained beyond the cap).
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const ids = Array.from({ length: 40 }, (_, i) => `r${99 - i}`);
        const load = vi.fn(async (id: string) => run(id, true));
        const out = await collectPipelineRuns(ids, 2, load);
        expect(out).toEqual([]);
        expect(load.mock.calls.length).toBeLessThanOrEqual(6);
        expect(warn).toHaveBeenCalledTimes(1);
        warn.mockRestore();
    });

    test("limit=0 and empty ids short-circuit without loading", async () => {
        const load = vi.fn(async (id: string) => run(id));
        expect(await collectPipelineRuns(["r1"], 0, load)).toEqual([]);
        expect(await collectPipelineRuns([], 5, load)).toEqual([]);
        expect(load).not.toHaveBeenCalled();
    });
});

describe("buildAdhocRows", () => {
    function adhocRun(
        id: string,
        startedAt: string | null,
        title: string | null = null,
    ): PerRun {
        return {
            id,
            overview: {
                id,
                tasks: [task({ taskId: "t" })],
                totalCostUsd: null,
                taskDurationSeconds: null,
                componentShas: [],
                startedAt,
            },
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: true,
            title,
        };
    }

    test("orders newest-first by run start_time, not by id", () => {
        // Ids sort lexically the opposite way to their dates — proving the sort
        // keys off start_time, not the id.
        const { rows } = buildAdhocRows(
            [
                adhocRun("aaa", "2026-06-01T08:00:00"),
                adhocRun("zzz", "2026-06-10T08:00:00"),
                adhocRun("mmm", "2026-06-05T08:00:00"),
            ],
            10,
        );
        expect(rows.map((r) => r.id)).toEqual(["zzz", "mmm", "aaa"]);
        expect(rows[0].startedAt).toBe("2026-06-10T08:00:00");
    });

    test("caps to the limit after sorting, total stays the full count", () => {
        // limit 2 shows the two newest but total still reports 3, so the UI can
        // offer "Show all (3)".
        const { rows, total } = buildAdhocRows(
            [
                adhocRun("a", "2026-06-01T08:00:00"),
                adhocRun("b", "2026-06-02T08:00:00"),
                adhocRun("c", "2026-06-03T08:00:00"),
            ],
            2,
        );
        expect(rows.map((r) => r.id)).toEqual(["c", "b"]);
        expect(total).toBe(3);
    });

    test("limit null returns every row uncapped", () => {
        const { rows, total } = buildAdhocRows(
            [
                adhocRun("a", "2026-06-01T08:00:00"),
                adhocRun("b", "2026-06-02T08:00:00"),
                adhocRun("c", "2026-06-03T08:00:00"),
            ],
            null,
        );
        expect(rows).toHaveLength(3);
        expect(total).toBe(3);
    });

    test("runs missing a start_time sort last, deterministic by id", () => {
        const { rows } = buildAdhocRows(
            [
                adhocRun("no-date-b", null),
                adhocRun("dated", "2026-06-01T08:00:00"),
                adhocRun("no-date-a", null),
            ],
            10,
        );
        // Dated run first; the two undated runs follow, id-descending.
        expect(rows.map((r) => r.id)).toEqual([
            "dated",
            "no-date-b",
            "no-date-a",
        ]);
    });

    test("drops runs without a readable overview", () => {
        const broken: PerRun = {
            id: "broken",
            overview: null,
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: true,
            title: null,
        };
        const { rows } = buildAdhocRows(
            [broken, adhocRun("ok", "2026-06-01T08:00:00")],
            10,
        );
        expect(rows.map((r) => r.id)).toEqual(["ok"]);
    });

    test("projects title and pass counts", () => {
        const run = adhocRun("r", "2026-06-01T08:00:00", "My run");
        run.overview!.tasks = [
            task({ taskId: "a", status: "SUCCESS" }),
            task({ taskId: "b", status: "FAILURE" }),
        ];
        const {
            rows: [row],
        } = buildAdhocRows([run], 10);
        expect(row.title).toBe("My run");
        expect(row.tasksSucceeded).toBe(1);
        expect(row.tasksRun).toBe(2);
    });
});
