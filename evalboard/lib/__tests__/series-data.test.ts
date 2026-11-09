import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SCRIBE_SOURCE, SOURCES, runsDirFor } from "@/lib/sources";

vi.mock("next/cache", () => ({
    unstable_cache: <T extends (...a: never[]) => unknown>(fn: T) => fn,
}));

const NOW = new Date("2026-11-10T09:00:00Z");

let localDir: string;

async function write(root: string, rel: string, body: unknown): Promise<void> {
    const abs = path.join(root, rel);
    await fs.mkdir(path.dirname(abs), { recursive: true });
    await fs.writeFile(abs, typeof body === "string" ? body : JSON.stringify(body));
}

function aggregate(passRate: number, over: Record<string, unknown> = {}) {
    return {
        tasks_run: 100,
        tasks_succeeded: Math.round(passRate * 100),
        tasks_failed: 100 - Math.round(passRate * 100),
        tasks_error: 3,
        tasks_graded: 100,
        pass_rate: passRate,
        average_duration: 120,
        ...over,
    };
}

async function twoArmRun(
    root: string,
    id: string,
    v1: number,
    v2: number,
    meta: Record<string, unknown>,
) {
    await write(root, `${id}/run.json`, { task_results: [] });
    await write(root, `${id}/meta.json`, { adhoc: true, ...meta });
    await write(root, `${id}/experiment.json`, {
        variant_ids: ["v1", "v2"],
        variant_aggregates: { v1: aggregate(v1), v2: aggregate(v2) },
    });
}

function taskRows(statuses: string[], cost = 1) {
    return statuses.map((status, i) => ({
        task_id: `t${i}`,
        status,
        total_cost_usd: cost,
        duration: 10,
    }));
}

async function singleArmRun(
    root: string,
    id: string,
    statuses: string[],
    meta: Record<string, unknown> | null,
) {
    await write(root, `${id}/run.json`, {
        start_time: `${id.slice(-19, -9)}T00:00:00`,
        task_results: taskRows(statuses),
    });
    if (meta) await write(root, `${id}/meta.json`, meta);
}

async function load() {
    vi.resetModules();
    return {
        data: await import("@/lib/series-data"),
        series: await import("@/lib/series"),
    };
}

beforeEach(async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    localDir = await fs.mkdtemp(path.join(os.tmpdir(), "series-data-"));
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", localDir);
    vi.stubEnv("EVALBOARD_RUNS_DIR", "");

    await twoArmRun(localDir, "adhoc-2026-11-09_16-18-40", 0.8364, 0.8084, {
        series: "maestro-v1-v2-codex",
        title: "Maestro v1 vs v2 · codex · 2026-11-09 · main",
    });
    await twoArmRun(localDir, "adhoc-2026-11-06_16-20-06", 0.8, 0.82, {
        series: "maestro-v1-v2-codex",
    });
    await twoArmRun(localDir, "adhoc-2026-11-05_16-30-19", 0.4, 0.44, {
        series: "maestro-v1-v2-codex",
        excluded: "infra: auth expired",
    });
    await twoArmRun(localDir, "adhoc-2026-11-04_16-00-00", 0.1, 0.1, {
        series: "n8n-vs-flow",
    });
    await singleArmRun(
        localDir,
        "adhoc-2026-11-09_06-21-12",
        ["SUCCESS", "SUCCESS", "FAILURE", "ERROR"],
        { adhoc: true, series: "ete-weekly" },
    );
    await singleArmRun(localDir, "adhoc-2026-11-04_10-00-00", ["SUCCESS"], {
        adhoc: true,
        title: "one-off experiment",
    });
    await singleArmRun(localDir, "adhoc-2026-11-03_10-00-00", ["SUCCESS", "FAILURE"], {
        adhoc: true,
        series: "brand-new",
    });
    await singleArmRun(localDir, "adhoc-2025-01-01_10-00-00", ["SUCCESS"], {
        adhoc: true,
        series: "ete-weekly",
    });

    const scribe = runsDirFor(localDir, SCRIBE_SOURCE);
    await singleArmRun(scribe, "2026-11-09_03-00-00", ["SUCCESS", "SUCCESS", "FAILURE"], null);
    await singleArmRun(scribe, "2026-11-06_03-00-00", ["SUCCESS", "FAILURE"], null);
    await singleArmRun(scribe, "2026-11-05_03-00-00", ["FAILURE"], { adhoc: true });
    await singleArmRun(scribe, "adhoc-2026-11-08_03-00-00", ["SUCCESS"], { adhoc: true });
});

afterEach(async () => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    await fs.rm(localDir, { recursive: true, force: true });
    for (const s of SOURCES) {
        await fs.rm(runsDirFor(localDir, s), { recursive: true, force: true });
    }
});

describe("loadSeriesMembers", () => {
    test("returns the series' runs newest first, excluded ones included", async () => {
        const { data, series } = await load();
        const members = await data.loadSeriesMembers(series.seriesByKey("maestro-v1-v2-codex"));
        expect(members.map((m) => m.id)).toEqual([
            "adhoc-2026-11-09_16-18-40",
            "adhoc-2026-11-06_16-20-06",
            "adhoc-2026-11-05_16-30-19",
        ]);
        expect(members[2].excluded).toBe("infra: auth expired");
        expect(members[0].excluded).toBeNull();
        expect(members[0].title).toBe("Maestro v1 vs v2 · codex · 2026-11-09 · main");
    });

    test("a run older than the look-back window is not read", async () => {
        const { data, series } = await load();
        const members = await data.loadSeriesMembers(series.seriesByKey("ete-weekly"));
        expect(members.map((m) => m.id)).toEqual(["adhoc-2026-11-09_06-21-12"]);
    });

    test("a canonical series takes the source's non-adhoc, date-shaped runs", async () => {
        const { data, series } = await load();
        const members = await data.loadSeriesMembers(series.seriesByKey("scribe"));
        expect(members.map((m) => m.id)).toEqual([
            "2026-11-09_03-00-00",
            "2026-11-06_03-00-00",
        ]);
    });

    test("the skills container's runs never leak into the Scribe series", async () => {
        const { data, series } = await load();
        const members = await data.loadSeriesMembers(series.seriesByKey("scribe"));
        expect(members.every((m) => !m.id.startsWith("adhoc-2026-11-09_16"))).toBe(true);
    });

    test("an unlisted key found in meta.json is reported", async () => {
        const { data } = await load();
        expect(await data.loadUnlistedSeriesKeys()).toEqual(["brand-new"]);
    });
});

describe("loadSeriesIndexRow", () => {
    test("a two-arm row reads experiment.json: latest, gap and sparks skip the excluded run", async () => {
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(series.seriesByKey("maestro-v1-v2-codex"));
        expect(row.arms.map((a) => a.id)).toEqual(["v1", "v2"]);
        expect(row.latest.v1).toBeCloseTo(83.64, 1);
        expect(row.latest.v2).toBeCloseTo(80.84, 1);
        expect(row.gap).toBeCloseTo(-2.8, 1);
        expect(row.sparks.v1).toEqual([80, expect.closeTo(83.64, 1)]);
        expect(row.sparks.v2).toEqual([82, expect.closeTo(80.84, 1)]);
        expect(row.runCount).toBe(3);
        expect(row.lastRun?.id).toBe("adhoc-2026-11-09_16-18-40");
    });

    test("the health pill counts the excluded run's slot as missed", async () => {
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(
            series.seriesByKey("maestro-v1-v2-codex"),
            NOW.getTime(),
        );
        expect(row.health).toEqual({ status: "missed", missed: 1, expected: 3 });
    });

    test("a two-arm row never reads run.json", async () => {
        const { data, series } = await load();
        const spy = vi.spyOn(fs, "readFile");
        await data.loadSeriesIndexRow(series.seriesByKey("maestro-v1-v2-codex"));
        const read = spy.mock.calls.map((c) => String(c[0]));
        spy.mockRestore();
        expect(read.some((p) => p.endsWith("run.json"))).toBe(false);
        expect(read.some((p) => p.endsWith("experiment.json"))).toBe(true);
    });

    test("a single-arm row takes its pass rate from the run overview", async () => {
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(series.seriesByKey("ete-weekly"));
        expect(row.arms).toEqual([series.SINGLE_ARM]);
        expect(row.latest.all).toBeCloseTo((2 / 4) * 100, 5);
        expect(row.gap).toBeNull();
        expect(row.sparks.all).toEqual([50]);
    });

    test("Scribe reads the aria container and skips excluded and adhoc runs", async () => {
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(series.seriesByKey("scribe"), NOW.getTime());
        expect(row.latest.all).toBeCloseTo((2 / 3) * 100, 5);
        expect(row.sparks.all).toEqual([50, expect.closeTo((2 / 3) * 100, 5)]);
        expect(row.lastRun?.id).toBe("2026-11-09_03-00-00");
    });

    test("a series with no runs yields an empty row, not an error", async () => {
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(series.seriesByKey("maestro-v1-v2-delegate"));
        expect(row.lastRun).toBeNull();
        expect(row.runCount).toBe(0);
        expect(row.health).toEqual({ status: "no-runs" });
        expect(row.arms.map((a) => a.id)).toEqual(["v1", "v2"]);
        expect(row.latest).toEqual({ v1: null, v2: null });
    });

    test("a run with no experiment.json is skipped rather than plotted as zero", async () => {
        await write(
            localDir,
            "adhoc-2026-11-09_19-00-00/run.json",
            { task_results: [] },
        );
        await write(localDir, "adhoc-2026-11-09_19-00-00/meta.json", {
            adhoc: true,
            series: "maestro-v1-v2-codex",
        });
        const { data, series } = await load();
        const row = await data.loadSeriesIndexRow(series.seriesByKey("maestro-v1-v2-codex"));
        expect(row.lastRun?.id).toBe("adhoc-2026-11-09_19-00-00");
        expect(row.latest.v1).toBeCloseTo(83.64, 1);
        expect(row.sparks.v1).toHaveLength(2);
    });
});

describe("loadSeriesDetail", () => {
    test("lists every run, keeps excluded ones' numbers, and plots the rest", async () => {
        const { data, series } = await load();
        const detail = await data.loadSeriesDetail(series.seriesByKey("maestro-v1-v2-codex"));
        expect(detail.total).toBe(3);
        expect(detail.runs.map((r) => r.ref.id)).toEqual([
            "adhoc-2026-11-09_16-18-40",
            "adhoc-2026-11-06_16-20-06",
            "adhoc-2026-11-05_16-30-19",
        ]);
        expect(detail.runs[2].stats?.arms.v1.passRate).toBeCloseTo(40, 5);
        expect(detail.points.map((p) => p.id)).toEqual([
            "adhoc-2026-11-09_16-18-40",
            "adhoc-2026-11-06_16-20-06",
        ]);
    });

    test("the limit caps the run list but reports the full count", async () => {
        const { data, series } = await load();
        const detail = await data.loadSeriesDetail(series.seriesByKey("maestro-v1-v2-codex"), 1);
        expect(detail.runs).toHaveLength(1);
        expect(detail.total).toBe(3);
    });

    test("an unknown series falls back to the run overview for its arm", async () => {
        const { data, series } = await load();
        const detail = await data.loadSeriesDetail(series.unknownSeries("brand-new"));
        expect(detail.arms).toEqual([series.SINGLE_ARM]);
        expect(detail.points).toHaveLength(1);
        expect(detail.points[0].values.all).toBe(50);
    });

    test("an unknown series with two-arm runs takes the arms from experiment.json", async () => {
        await twoArmRun(localDir, "adhoc-2026-11-08_10-00-00", 0.5, 0.6, { series: "brand-new" });
        const { data, series } = await load();
        const detail = await data.loadSeriesDetail(series.unknownSeries("brand-new"));
        const ids = detail.points.flatMap((p) => Object.keys(p.values));
        expect(ids).toContain("v1");
        expect(detail.arms.map((a) => a.id)).toEqual(expect.arrayContaining(["v1", "v2", "all"]));
    });
});

describe("runLevel", () => {
    test("cost per task, error share and agent time per passed task", async () => {
        const { data } = await load();
        const level = data.runLevel({
            id: "r",
            tasks: ["SUCCESS", "SUCCESS", "FAILURE", "ERROR", "NOT_GRADED"].map((status, i) => ({
                taskId: `t${i}`,
                status,
                tags: [],
                skill: null,
                totalCostUsd: 1,
                durationSeconds: 100,
                agentSeconds: 60,
                weightedScore: null,
                actualCommands: null,
                totalTurns: null,
                expectedTurns: null,
                expectedSeconds: null,
                visibleTurns: null,
                hasFinalReply: false,
            })),
            totalCostUsd: 10,
            taskDurationSeconds: 500,
            componentShas: [],
        });
        expect(level?.costPerTask).toBe(2);
        expect(level?.errorShare).toBeCloseTo(25, 5);
        expect(level?.timePerPassedTask).toBeCloseTo(150, 5);
    });

    test("no tasks, no numbers", async () => {
        const { data } = await load();
        expect(data.runLevel(null)).toBeNull();
        expect(
            data.runLevel({
                id: "r",
                tasks: [],
                totalCostUsd: null,
                taskDurationSeconds: null,
                componentShas: [],
            }),
        ).toBeNull();
    });
});
