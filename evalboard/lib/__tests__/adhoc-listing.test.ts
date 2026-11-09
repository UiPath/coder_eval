import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SOURCES, runsDirFor } from "@/lib/sources";

vi.mock("next/cache", () => ({
    unstable_cache: <T extends (...a: never[]) => unknown>(fn: T) => fn,
}));

let localDir: string;

async function addRun(
    id: string,
    meta: Record<string, unknown> | null,
    startTime = `${id.slice(-19, -9)}T12:00:00`,
): Promise<void> {
    const dir = path.join(localDir, id);
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(
        path.join(dir, "run.json"),
        JSON.stringify({
            start_time: startTime,
            task_results: [{ task_id: "t", status: "SUCCESS" }],
        }),
    );
    if (meta) await fs.writeFile(path.join(dir, "meta.json"), JSON.stringify(meta));
}

async function listing(limit: number | null) {
    vi.resetModules();
    const { getAdhocRunListing } = await import("@/lib/overview");
    return getAdhocRunListing(limit);
}

beforeEach(async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-10T09:00:00Z"));
    localDir = await fs.mkdtemp(path.join(os.tmpdir(), "adhoc-listing-"));
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", localDir);
    vi.stubEnv("EVALBOARD_RUNS_DIR", "");
});

afterEach(async () => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    await fs.rm(localDir, { recursive: true, force: true });
    for (const s of SOURCES) {
        await fs.rm(runsDirFor(localDir, s), { recursive: true, force: true });
    }
});

describe("getAdhocRunListing", () => {
    test("drops runs that belong to a series, excluded ones too", async () => {
        await addRun("adhoc-2026-11-08_10-00-00", { adhoc: true, title: "one-off" });
        await addRun("adhoc-2026-11-08_16-00-00", { adhoc: true, series: "maestro-v1-v2-codex" });
        await addRun("adhoc-2026-11-07_16-00-00", {
            adhoc: true,
            series: "maestro-v1-v2-codex",
            excluded: "infra: auth expired",
        });
        const { rows, total } = await listing(10);
        expect(rows.map((r) => r.id)).toEqual(["adhoc-2026-11-08_10-00-00"]);
        expect(total).toBe(1);
    });

    test("lists a date-shaped run flagged adhoc that has no series", async () => {
        await addRun("2026-11-04_20-19-37", { adhoc: true, title: "demoted nightly" });
        await addRun("2026-11-08_03-00-00", null);
        await addRun("adhoc-2026-11-08_10-00-00", { adhoc: true });
        const { rows } = await listing(10);
        expect(rows.map((r) => r.id)).toEqual([
            "adhoc-2026-11-08_10-00-00",
            "2026-11-04_20-19-37",
        ]);
        expect(rows[1].title).toBe("demoted nightly");
    });

    test("drops a date-shaped adhoc run that carries a series", async () => {
        await addRun("2026-11-05_03-00-00", { adhoc: true, series: "scribe" });
        await addRun("2026-11-04_20-19-37", { adhoc: true });
        const { rows } = await listing(10);
        expect(rows.map((r) => r.id)).toEqual(["2026-11-04_20-19-37"]);
    });

    test("a canonical nightly with no meta stays out of the list", async () => {
        await addRun("2026-11-08_03-00-00", null);
        const { rows, total } = await listing(10);
        expect(rows).toEqual([]);
        expect(total).toBe(0);
    });

    test("a run with an undated id is still listed", async () => {
        await addRun("custom-run-name", null, "2026-10-01T00:00:00");
        const { rows } = await listing(10);
        expect(rows.map((r) => r.id)).toEqual(["custom-run-name"]);
    });

    test("series runs crowding the newest candidates do not shorten the list", async () => {
        for (let i = 0; i < 14; i++) {
            const h = String(i).padStart(2, "0");
            await addRun(`adhoc-2026-11-09_${h}-00-00`, {
                adhoc: true,
                series: "ete-weekly",
            });
        }
        for (let i = 0; i < 3; i++) {
            await addRun(`adhoc-2026-10-2${i}_10-00-00`, { adhoc: true });
        }
        const { rows, total } = await listing(2);
        expect(rows.map((r) => r.id)).toEqual([
            "adhoc-2026-10-22_10-00-00",
            "adhoc-2026-10-21_10-00-00",
        ]);
        expect(total).toBe(3);
    });

    test("total counts what was never loaded when the load is cut short", async () => {
        for (let i = 0; i < 30; i++) {
            const d = String(i + 1).padStart(2, "0");
            await addRun(`adhoc-2026-10-${d}_10-00-00`, { adhoc: true });
        }
        const { rows, total } = await listing(2);
        expect(rows).toHaveLength(2);
        expect(total).toBe(30);
    });
});
