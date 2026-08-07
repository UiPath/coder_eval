import { describe, expect, test, vi } from "vitest";
import {
    buildAdhocRows,
    buildTagTaskRows,
    collectPipelineRuns,
    projectRunRow,
    scopeRunTasks,
    summarizeListing,
    taskCarriesRepoTag,
    taskMatchesTag,
    turnBudgetRateForTasks,
    type PerRun,
    type RunListingRow,
} from "../overview";
import { normalizeHarness } from "../harness";
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

describe("summarizeListing", () => {
    function row(overrides: Partial<RunListingRow>): RunListingRow {
        return {
            id: "r",
            tasksSucceeded: 0,
            tasksRun: 0,
            totalCostUsd: null,
            taskDurationSeconds: null,
            ...overrides,
        };
    }

    test("empty listing has null cost/duration and no partial flags", () => {
        const t = summarizeListing([]);
        expect(t).toEqual({
            costUsd: null,
            costPartial: false,
            tasksSucceeded: 0,
            tasksRun: 0,
            durationSeconds: null,
            durationPartial: false,
        });
    });

    test("sums cost, duration, and task counts across runs", () => {
        const t = summarizeListing([
            row({
                tasksSucceeded: 8,
                tasksRun: 10,
                totalCostUsd: 1.5,
                taskDurationSeconds: 120,
            }),
            row({
                tasksSucceeded: 3,
                tasksRun: 5,
                totalCostUsd: 2.25,
                taskDurationSeconds: 60,
            }),
        ]);
        expect(t.costUsd).toBeCloseTo(3.75);
        expect(t.durationSeconds).toBe(180);
        expect(t.tasksSucceeded).toBe(11);
        expect(t.tasksRun).toBe(15);
        expect(t.costPartial).toBe(false);
        expect(t.durationPartial).toBe(false);
    });

    test("flags partial when a run lacks cost or duration", () => {
        // One run recorded cost/duration, one didn't: sum reflects only the
        // recorded run and the *Partial flags say so.
        const t = summarizeListing([
            row({ totalCostUsd: 4, taskDurationSeconds: 30 }),
            row({ totalCostUsd: null, taskDurationSeconds: null }),
        ]);
        expect(t.costUsd).toBe(4);
        expect(t.costPartial).toBe(true);
        expect(t.durationSeconds).toBe(30);
        expect(t.durationPartial).toBe(true);
    });

    test("all-missing cost stays null, not zero", () => {
        // A window where no run has a cost must read "—", not "$0.00" — the
        // sum is unknown, not zero. Same for duration.
        const t = summarizeListing([row({}), row({})]);
        expect(t.costUsd).toBeNull();
        expect(t.costPartial).toBe(false);
        expect(t.durationSeconds).toBeNull();
        expect(t.durationPartial).toBe(false);
    });
});

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

    test("probeAll reaches a match sitting past the scan cap", async () => {
        // The paged run table derives "Show more" from finding one extra row, so
        // a scan that gave up at the cap would report "no more" and strand every
        // older matching run. Only r1 matches, 30 ids deep — well past
        // limit x HARNESS_SCAN_FACTOR.
        const ids = Array.from({ length: 30 }, (_, i) => `r${30 - i}`);
        const load = vi.fn(async (id: string) => run(id));
        const isMatch = (r: PerRun) => r.id === "r1";

        expect(await collectPipelineRuns(ids, 1, load, isMatch)).toEqual([]);
        const out = await collectPipelineRuns(ids, 1, load, isMatch, true);
        expect(out.map((r) => r.id)).toEqual(["r1"]);
    });

    test("limit=0 and empty ids short-circuit without loading", async () => {
        const load = vi.fn(async (id: string) => run(id));
        expect(await collectPipelineRuns(["r1"], 0, load)).toEqual([]);
        expect(await collectPipelineRuns([], 5, load)).toEqual([]);
        expect(load).not.toHaveBeenCalled();
    });

    // A usable run tagged with a specific harness.
    function runH(id: string, harness: string): PerRun {
        const r = run(id);
        return { ...r, overview: { ...r.overview!, harness } };
    }

    test("isMatch filters to the matching harness and backfills the rest", async () => {
        // Interleaved harnesses; scoping to claude-code must skip the codex
        // runs and reach further back to fill the window rather than returning
        // short.
        const harnesses: Record<string, string> = {
            r6: "claude-code",
            r5: "codex",
            r4: "claude-code",
            r3: "codex",
            r2: "claude-code",
            r1: "codex",
        };
        const load = vi.fn(async (id: string) => runH(id, harnesses[id]));
        const out = await collectPipelineRuns(
            ["r6", "r5", "r4", "r3", "r2", "r1"],
            3,
            load,
            (r) => r.overview?.harness === "claude-code",
        );
        expect(out.map((r) => r.id)).toEqual(["r6", "r4", "r2"]);
    });

    test("harness scan reaches past RECENT_SCAN_FACTOR to gather a rare harness", async () => {
        // limit 1; the only antigravity run sits at index 5 — beyond the
        // unfiltered cap (1 × RECENT_SCAN_FACTOR = 3). The wider harness cap
        // (1 × HARNESS_SCAN_FACTOR = 8) must reach it.
        const ids = ["r6", "r5", "r4", "r3", "r2", "r1"];
        const load = vi.fn(async (id: string) =>
            runH(id, id === "r1" ? "antigravity" : "claude-code"),
        );
        const out = await collectPipelineRuns(
            ids,
            1,
            load,
            (r) => r.overview?.harness === "antigravity",
        );
        expect(out.map((r) => r.id)).toEqual(["r1"]);
    });
});

describe("normalizeHarness", () => {
    test("null/undefined fold to claude-code (legacy pre-stamp runs)", () => {
        expect(normalizeHarness(null)).toBe("claude-code");
        expect(normalizeHarness(undefined)).toBe("claude-code");
    });
    test("an explicit harness passes through unchanged", () => {
        expect(normalizeHarness("codex")).toBe("codex");
        expect(normalizeHarness("antigravity")).toBe("antigravity");
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

// buildTagTaskRows drives /path-to-ga. Two behaviours are asserted here that no
// other test covers: a task de-tagged upstream (present in a newer run WITHOUT
// the tag) must disappear, and a mature carry-forward must leave both the
// numerator AND the denominator of passRate — the page-local divergence from
// trends.ts, which counts a carry-forward as a pass.
describe("buildTagTaskRows", () => {
    const TAG = "path-to-ga";

    function perRun(id: string, tasks: RunOverviewTask[]): PerRun {
        return {
            id,
            overview: {
                id,
                tasks,
                totalCostUsd: null,
                taskDurationSeconds: null,
                componentShas: [],
            },
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: false,
            title: null,
        };
    }

    test("keeps a task still tagged in the newest run", () => {
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", tags: [TAG] })]),
                perRun("r2", [task({ taskId: "a", tags: [TAG] })]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a"]);
        expect(rows[0].appearances).toBe(2);
        expect(rows[0].latestRunId).toBe("r2");
    });

    test("drops a task present-but-untagged in a newer run (the de-tag bug)", () => {
        // Models ipe-drive-to-slack: tagged in r1, still running in the newer r2
        // but with the tag removed from its YAML. That is proof of de-tagging, so
        // the row must vanish rather than linger until r1 ages out of the window.
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "detagged", tags: [TAG] })]),
                perRun("r2", [task({ taskId: "detagged", tags: ["other"] })]),
            ],
            TAG,
        );
        expect(rows).toEqual([]);
    });

    test("keeps a task that simply stopped appearing, dated to its newest tagged run", () => {
        // Retired / renamed / skip:true is unknowable from run data, so the row
        // stays and latestRunId is what the page renders as "Last seen".
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "gone", tags: [TAG] })]),
                perRun("r2", [task({ taskId: "other", tags: [TAG] })]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["gone", "other"]);
        expect(rows[0].latestRunId).toBe("r1");
    });

    test("one untagged replicate in the newest run is not a de-tag", () => {
        // A replicated task has several rows per run; the de-tag check collapses
        // with any-row semantics, so a single untagged replicate cannot drop it.
        const rows = buildTagTaskRows(
            [
                perRun("r1", [
                    task({ taskId: "a", tags: [TAG] }),
                    task({ taskId: "a", tags: [] }),
                ]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a"]);
        // Only the tagged row accumulates.
        expect(rows[0].appearances).toBe(1);
    });

    test("matureSkips counts carry-forwards; appearances still includes them", () => {
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", tags: [TAG] })]),
                perRun("r2", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
            ],
            TAG,
        );
        expect(rows[0].appearances).toBe(2);
        expect(rows[0].matureSkips).toBe(1);
    });

    test("passRate excludes mature skips from both numerator and denominator", () => {
        // 4 appearances, 1 mature skip, 2 executed passes out of 3 executed rows
        // → 66.67%. The old mature-inclusive rule would have said 75% (3/4).
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", tags: [TAG] })]),
                perRun("r2", [
                    task({ taskId: "a", tags: [TAG], status: "FAILURE" }),
                ]),
                perRun("r3", [task({ taskId: "a", tags: [TAG] })]),
                perRun("r4", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
            ],
            TAG,
        );
        expect(rows[0].appearances).toBe(4);
        expect(rows[0].matureSkips).toBe(1);
        expect(rows[0].passRate).toBeCloseTo(66.6667, 3);
    });

    test("passRate is null when every tagged appearance was a mature skip", () => {
        // Nothing was measured, so the page must show "—" rather than a
        // measured-looking 100% (or a divide-by-zero NaN).
        const rows = buildTagTaskRows(
            [
                perRun("r1", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
                perRun("r2", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
            ],
            TAG,
        );
        expect(rows[0].matureSkips).toBe(2);
        expect(rows[0].passRate).toBeNull();
    });

    test("latestMatureSkipped reflects the newest tagged appearance", () => {
        const skippedLatest = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", tags: [TAG] })]),
                perRun("r2", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
            ],
            TAG,
        );
        expect(skippedLatest[0].latestMatureSkipped).toBe(true);

        const executedLatest = buildTagTaskRows(
            [
                perRun("r1", [
                    task({ taskId: "a", tags: [TAG], matureSkipped: true }),
                ]),
                perRun("r2", [task({ taskId: "a", tags: [TAG] })]),
            ],
            TAG,
        );
        expect(executedLatest[0].latestMatureSkipped).toBe(false);
    });

    test("a tag matched via skill is never dropped", () => {
        // taskCarriesRepoTag's first clause: every run containing the task
        // matches, so `tagged` is always true. Using a raw tags.includes() here
        // would wrongly drop every skill-tag row.
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", skill: TAG })]),
                perRun("r2", [task({ taskId: "a", skill: TAG })]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a"]);
    });

    test("a task carrying the tag only as a review tag does not appear", () => {
        // Review tags are a post-hoc, effectively disjoint namespace; this page
        // reports repo-declared tags only.
        const r = perRun("r1", [task({ taskId: "a", tags: [] })]);
        const rows = buildTagTaskRows(
            [{ ...r, reviewTagsByTask: { a: [TAG] } }],
            TAG,
        );
        expect(rows).toEqual([]);
    });

    test("a review-tagged older appearance is not folded into a kept row", () => {
        // Discriminates the accumulate path specifically: the row IS kept (its
        // newest run carries the repo tag), so only `appearances` reveals which
        // predicate accumulated. Widening the accumulate path back to
        // taskMatchesTag would count r1's review-tag-only row too and report 2.
        const older = perRun("r1", [task({ taskId: "a", tags: [] })]);
        const rows = buildTagTaskRows(
            [
                { ...older, reviewTagsByTask: { a: [TAG] } },
                perRun("r2", [task({ taskId: "a", tags: [TAG] })]),
            ],
            TAG,
        );
        expect(rows).toHaveLength(1);
        expect(rows[0].appearances).toBe(1);
    });

    test("a task that only recently GAINED the tag is kept", () => {
        // Mirror image of the de-tag case, and the reason the newest-appearance
        // rule is first-write-wins rather than "tagged in every appearance": a
        // task tagged on day 20 of a 30-day window is present-but-untagged in the
        // older runs, and must not read as de-tagged.
        const rows = buildTagTaskRows(
            [
                perRun("r1", [task({ taskId: "a", tags: [] })]),
                perRun("r2", [task({ taskId: "a", tags: [TAG] })]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a"]);
        expect(rows[0].appearances).toBe(1);
        expect(rows[0].latestRunId).toBe("r2");
    });

    test("a newest run with a null overview neither adds nor drops anything", () => {
        // A transient blob failure on the newest run must not read as a de-tag of
        // every row.
        const broken: PerRun = {
            id: "r9",
            overview: null,
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: false,
            title: null,
        };
        const rows = buildTagTaskRows(
            [broken, perRun("r1", [task({ taskId: "a", tags: [TAG] })])],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a"]);
        expect(rows[0].appearances).toBe(1);
        expect(rows[0].latestRunId).toBe("r1");
    });

    test("empty input returns an empty list", () => {
        expect(buildTagTaskRows([], TAG)).toEqual([]);
    });

    test("rows come back sorted by taskId", () => {
        const rows = buildTagTaskRows(
            [
                perRun("r1", [
                    task({ taskId: "c", tags: [TAG] }),
                    task({ taskId: "a", tags: [TAG] }),
                    task({ taskId: "b", tags: [TAG] }),
                ]),
            ],
            TAG,
        );
        expect(rows.map((r) => r.taskId)).toEqual(["a", "b", "c"]);
    });

    test("all three latest* fields come off the same row on replicate disagreement", () => {
        // The newest run has an executed replicate and a carried-forward one.
        // First-row-wins decides, and the Mature pill must never sit beside a
        // measured score from the other replicate.
        const rows = buildTagTaskRows(
            [
                perRun("r1", [
                    task({
                        taskId: "a",
                        tags: [TAG],
                        matureSkipped: true,
                        status: "SUCCESS",
                        weightedScore: 1.0,
                    }),
                    task({
                        taskId: "a",
                        tags: [TAG],
                        status: "FAILURE",
                        weightedScore: 0.25,
                    }),
                ]),
            ],
            TAG,
        );
        expect(rows[0].latestMatureSkipped).toBe(true);
        expect(rows[0].latestStatus).toBe("SUCCESS");
        expect(rows[0].latestScore).toBe(1.0);
        // Both replicates are tagged, so `appearances` counts ROWS (2), not runs
        // (1) — the semantics the interface comment promises.
        expect(rows[0].appearances).toBe(2);
        expect(rows[0].matureSkips).toBe(1);
    });
});

// taskMatchesTag was rebuilt on top of the extracted taskCarriesRepoTag so
// buildTagTaskRows could reuse the repo-provenance half. scopeRunTasks and the
// front-page rails still go through taskMatchesTag and legitimately filter on
// review tags, so the extraction has to be behaviour-preserving.
describe("taskCarriesRepoTag / taskMatchesTag", () => {
    test("taskCarriesRepoTag matches skill and YAML tags, not review tags", () => {
        expect(taskCarriesRepoTag(task({ skill: "x" }), "x")).toBe(true);
        expect(taskCarriesRepoTag(task({ tags: ["x"] }), "x")).toBe(true);
        expect(taskCarriesRepoTag(task({ tags: ["y"] }), "x")).toBe(false);
    });

    test("taskMatchesTag still matches via skill, tags, or a review tag", () => {
        expect(taskMatchesTag(task({ skill: "x" }), {}, "x")).toBe(true);
        expect(taskMatchesTag(task({ tags: ["x"] }), {}, "x")).toBe(true);
        expect(
            taskMatchesTag(task({ taskId: "t" }), { t: ["x"] }, "x"),
        ).toBe(true);
        expect(taskMatchesTag(task({ taskId: "t", tags: ["y"] }), {}, "x")).toBe(
            false,
        );
    });
});

// projectRunRow is the single definition of "does this run count, and with which
// tasks" — the summary tiles (getWindowRollup) and the paged run table
// (getRunListing) both go through it. They used to be one loop; if they ever
// disagreed, the tiles would describe a different set of runs than the table
// below them with nothing failing.
describe("projectRunRow", () => {
    function run(id: string, tasks: RunOverviewTask[], extra?: Partial<PerRun>): PerRun {
        return {
            id,
            overview: {
                id,
                tasks,
                totalCostUsd: 10,
                taskDurationSeconds: 100,
                componentShas: [],
            },
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: false,
            title: null,
            ...extra,
        };
    }

    test("unfiltered, reports whole-run totals", () => {
        const row = projectRunRow(
            run("r", [
                task({ taskId: "a", status: "SUCCESS" }),
                task({ taskId: "b", status: "FAILURE" }),
            ]),
            null,
            null,
        );
        expect(row).toMatchObject({
            id: "r",
            tasksSucceeded: 1,
            tasksRun: 2,
            totalCostUsd: 10,
            taskDurationSeconds: 100,
        });
    });

    test("drops a run with no task matching the tag", () => {
        const row = projectRunRow(
            run("r", [task({ taskId: "a", tags: ["smoke"] })]),
            "path-to-ga",
            null,
        );
        expect(row).toBeNull();
    });

    test("scopes cost and duration to the matching tasks", () => {
        // The row's numbers have to describe the filtered slice, not the run —
        // otherwise filtering to one tag still shows the whole run's spend.
        const row = projectRunRow(
            run("r", [
                task({
                    taskId: "a",
                    tags: ["keep"],
                    totalCostUsd: 3,
                    durationSeconds: 30,
                }),
                task({ taskId: "b", totalCostUsd: 99, durationSeconds: 999 }),
            ]),
            "keep",
            null,
        );
        expect(row).toMatchObject({
            tasksRun: 1,
            totalCostUsd: 3,
            taskDurationSeconds: 30,
        });
    });

    test("nulls a scoped duration when any matching task lacks one", () => {
        // A partial sum understates the slice, which reads as a fast run rather
        // than an unmeasured one.
        const row = projectRunRow(
            run("r", [
                task({ taskId: "a", tags: ["keep"], durationSeconds: 30 }),
                task({ taskId: "b", tags: ["keep"], durationSeconds: null }),
            ]),
            "keep",
            null,
        );
        expect(row?.taskDurationSeconds).toBeNull();
    });

    test("a query matching only the run id keeps the whole-run slice", () => {
        // Pinning a run by pasting a date fragment must not show 0 tasks just
        // because no task name contains the date.
        const row = projectRunRow(
            run("2026-07-30_04-38-11", [task({ taskId: "a" })]),
            null,
            "07-30",
        );
        expect(row).toMatchObject({ tasksRun: 1 });
    });

    test("returns null for a run whose run.json could not be read", () => {
        const r = run("r", []);
        expect(projectRunRow({ ...r, overview: null }, null, null)).toBeNull();
    });

    test("carries the harness through for the table's badge column", () => {
        const r = run("r", [task({ taskId: "a" })]);
        const withHarness = {
            ...r,
            overview: { ...r.overview!, harness: "codex" },
        };
        expect(projectRunRow(withHarness, null, null)?.harness).toBe("codex");
    });

    // getOverview reads the task LIST out of the same call (for the turn-budget
    // rate), while the table only ever sees the counts. If the two disagreed
    // about which tasks are in scope, the charts and the tiles would describe
    // different runs with nothing on the page to show it.
    describe("scopeRunTasks, the seam both share", () => {
        test("hands back the matching tasks themselves, not just a count", () => {
            const scoped = scopeRunTasks(
                run("r", [
                    task({ taskId: "keep-me", tags: ["keep"] }),
                    task({ taskId: "drop-me" }),
                ]),
                "keep",
                null,
            );
            expect(scoped?.tasks.map((t) => t.taskId)).toEqual(["keep-me"]);
        });

        test("agrees with projectRunRow on which runs are in scope", () => {
            const r = run("r", [task({ taskId: "a", tags: ["other"] })]);
            expect(scopeRunTasks(r, "keep", null)).toBeNull();
            expect(projectRunRow(r, "keep", null)).toBeNull();
        });

        test("the run-id fallback widens back to every task", () => {
            const scoped = scopeRunTasks(
                run("2026-07-30_04-38-11", [
                    task({ taskId: "a" }),
                    task({ taskId: "b" }),
                ]),
                null,
                "07-30",
            );
            expect(scoped?.tasks).toHaveLength(2);
        });
    });
});
