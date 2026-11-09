import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
    parseExcludedReason,
    parseExperiment,
    parseSeriesKey,
} from "../runs";

describe("parseSeriesKey", () => {
    test("accepts a path-safe key and rejects anything else", () => {
        expect(parseSeriesKey("maestro-v1-v2-codex")).toBe("maestro-v1-v2-codex");
        expect(parseSeriesKey("")).toBeNull();
        expect(parseSeriesKey("a/b")).toBeNull();
        expect(parseSeriesKey("../x")).toBeNull();
        expect(parseSeriesKey("x".repeat(65))).toBeNull();
        expect(parseSeriesKey(7)).toBeNull();
        expect(parseSeriesKey(null)).toBeNull();
    });
});

describe("parseExcludedReason", () => {
    test("keeps a trimmed reason, drops blanks and non-strings", () => {
        expect(parseExcludedReason("  infra: auth expired ")).toBe("infra: auth expired");
        expect(parseExcludedReason("   ")).toBeNull();
        expect(parseExcludedReason(true)).toBeNull();
        expect(parseExcludedReason(undefined)).toBeNull();
    });

    test("caps a runaway reason", () => {
        expect(parseExcludedReason("x".repeat(2000))).toHaveLength(500);
    });
});

describe("parseExperiment", () => {
    const agg = {
        tasks_run: 214,
        tasks_succeeded: 179,
        tasks_failed: 15,
        tasks_error: 20,
        tasks_graded: 214,
        pass_rate: 0.8364485981308412,
        average_duration: 143.4,
    };

    test("reads the arms in variant_ids order, pass rate as a percent", () => {
        const exp = parseExperiment({
            variant_ids: ["v2", "v1"],
            variant_aggregates: { v1: agg, v2: { ...agg, pass_rate: 0.5 } },
        });
        expect(exp?.variantIds).toEqual(["v2", "v1"]);
        expect(exp?.arms.v1).toMatchObject({
            variantId: "v1",
            tasksRun: 214,
            tasksSucceeded: 179,
            tasksFailed: 15,
            tasksError: 20,
            tasksGraded: 214,
            averageDuration: 143.4,
        });
        expect(exp?.arms.v1.passRate).toBeCloseTo(83.64, 1);
        expect(exp?.arms.v2.passRate).toBe(50);
    });

    test("derives the rate from the counts when pass_rate is absent", () => {
        const exp = parseExperiment({
            variant_aggregates: {
                a: { tasks_succeeded: 3, tasks_graded: 4 },
                b: { tasks_succeeded: 0, tasks_graded: 0 },
            },
        });
        expect(exp?.variantIds).toEqual(["a", "b"]);
        expect(exp?.arms.a.passRate).toBe(75);
        expect(exp?.arms.b.passRate).toBeNull();
    });

    test("an explicit null pass_rate stays null instead of reading as 0%", () => {
        const exp = parseExperiment({
            variant_aggregates: {
                a: { tasks_succeeded: 0, tasks_error: 3, tasks_graded: 3, pass_rate: null },
            },
        });
        expect(exp?.arms.a.passRate).toBeNull();
    });

    test("an aggregate not named in variant_ids is still an arm", () => {
        const exp = parseExperiment({
            variant_ids: ["v1"],
            variant_aggregates: { v1: agg, v2: agg },
        });
        expect(exp?.variantIds).toEqual(["v1", "v2"]);
    });

    test("a missing or malformed file reads as no experiment", () => {
        expect(parseExperiment(null)).toBeNull();
        expect(parseExperiment({})).toBeNull();
        expect(parseExperiment({ variant_aggregates: null })).toBeNull();
        expect(parseExperiment({ variant_aggregates: {} })).toBeNull();
        expect(
            parseExperiment({ variant_aggregates: { "../x": agg } }),
        ).toBeNull();
    });
});

describe("readRunMeta", () => {
    let dir: string;

    beforeEach(async () => {
        dir = await fs.mkdtemp(path.join(os.tmpdir(), "run-meta-"));
        vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", dir);
        vi.resetModules();
    });

    afterEach(async () => {
        vi.unstubAllEnvs();
        await fs.rm(dir, { recursive: true, force: true });
    });

    async function meta(body: unknown, id = "adhoc-2026-11-09_16-18-40") {
        await fs.mkdir(path.join(dir, id), { recursive: true });
        await fs.writeFile(path.join(dir, id, "meta.json"), JSON.stringify(body));
        const { readRunMeta } = await import("../runs");
        return readRunMeta(id);
    }

    test("reads series and excluded next to adhoc and title", async () => {
        expect(
            await meta({
                adhoc: true,
                series: "maestro-v1-v2-codex",
                excluded: "replaced by adhoc-2026-11-10_08-12-03",
                title: "T",
            }),
        ).toEqual({
            adhoc: true,
            title: "T",
            description: null,
            series: "maestro-v1-v2-codex",
            excluded: "replaced by adhoc-2026-11-10_08-12-03",
        });
    });

    test("a meta.json from before these fields reads as no series, not excluded", async () => {
        expect(await meta({ adhoc: true, title: "T" })).toMatchObject({
            adhoc: true,
            series: null,
            excluded: null,
        });
    });

    test("unknown fields and wrong-typed values are tolerated", async () => {
        expect(
            await meta({ adhoc: "yes", series: 3, excluded: {}, future: [1] }),
        ).toMatchObject({ adhoc: false, series: null, excluded: null });
    });

    test("no meta.json reads as null", async () => {
        await fs.mkdir(path.join(dir, "r1"), { recursive: true });
        const { readRunMeta } = await import("../runs");
        expect(await readRunMeta("r1")).toBeNull();
    });
});
