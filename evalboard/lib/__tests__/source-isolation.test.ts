import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SCRIBE_SOURCE } from "@/lib/sources";

// The invariant these tests pin: run ids are only unique WITHIN a container.
// Both suites name runs `YYYY-MM-DD_HH-MM-SS`, so a same-day skills run and
// aria run collide on id, and a source-blind reader renders one as the other —
// a silently wrong answer rather than a 404.
//
// The rest of the source tests cover the registry, `runsDirFor`'s string math,
// and href building. None of them touch the READER layer, which is where the
// invariant actually has to hold: a future reader added without the trailing
// `source` param — or one reaching for RUNS_DIR directly — would read the wrong
// container and leave every one of those tests passing. This file is the one
// that fails in that case.
//
// Local mode (EVALBOARD_LOCAL_RUNS_DIR) is the backend under test because it
// needs no blob credentials and no network: every ensure* is a no-op, so the
// readers exercise pure path resolution. It is also the backend where this bug
// actually shipped — `listRunIdsRemote` ignored its container argument in local
// mode, so /scribe listed the SKILLS tree's ids while every read resolved under
// the -scribe sibling.
const RUN_ID = "2026-08-14_12-00-00";

// Minimal run.json, distinguishable per source by every field asserted below.
function runJson(opts: {
    tasksRun: number;
    tasksSucceeded: number;
    startTime: string;
}) {
    return JSON.stringify({
        start_time: opts.startTime,
        tasks_run: opts.tasksRun,
        tasks_succeeded: opts.tasksSucceeded,
        task_results: [],
    });
}

// RUNS_DIR is a module-level const read from env at import time, so each
// scenario sets env, resets the registry, then imports a fresh copy.
async function loadRuns() {
    vi.resetModules();
    return import("@/lib/runs");
}

const ENV_KEYS = ["EVALBOARD_LOCAL_RUNS_DIR", "EVALBOARD_RUNS_DIR"] as const;
let savedEnv: Record<string, string | undefined>;
let localDir: string;

beforeEach(async () => {
    savedEnv = {};
    for (const k of ENV_KEYS) savedEnv[k] = process.env[k];

    localDir = await fs.mkdtemp(path.join(os.tmpdir(), "source-iso-"));
    process.env.EVALBOARD_LOCAL_RUNS_DIR = localDir;
    delete process.env.EVALBOARD_RUNS_DIR;

    // Same run id in both trees, different contents — the collision case.
    const skillsRun = path.join(localDir, RUN_ID);
    const scribeRun = path.join(`${localDir}-scribe`, RUN_ID);
    await fs.mkdir(skillsRun, { recursive: true });
    await fs.mkdir(scribeRun, { recursive: true });
    await fs.writeFile(
        path.join(skillsRun, "run.json"),
        runJson({
            tasksRun: 100,
            tasksSucceeded: 90,
            startTime: "2026-08-14T12:00:00Z",
        }),
    );
    await fs.writeFile(
        path.join(scribeRun, "run.json"),
        runJson({
            tasksRun: 2,
            tasksSucceeded: 0,
            startTime: "2026-08-14T18:00:00Z",
        }),
    );
});

afterEach(async () => {
    for (const k of ENV_KEYS) {
        if (savedEnv[k] === undefined) delete process.env[k];
        else process.env[k] = savedEnv[k];
    }
    await fs.rm(localDir, { recursive: true, force: true });
    await fs.rm(`${localDir}-scribe`, { recursive: true, force: true });
});

describe("reader-layer source isolation", () => {
    test("readRunSummary resolves the same id to different runs per source", async () => {
        const { readRunSummary } = await loadRuns();

        const skills = await readRunSummary(RUN_ID);
        const scribe = await readRunSummary(RUN_ID, SCRIBE_SOURCE);

        // Both exist — this is a collision, not a miss.
        expect(skills).not.toBeNull();
        expect(scribe).not.toBeNull();

        expect(skills?.tasksRun).toBe(100);
        expect(skills?.tasksSucceeded).toBe(90);
        expect(scribe?.tasksRun).toBe(2);
        expect(scribe?.tasksSucceeded).toBe(0);
        // Same id, different run — the whole point.
        expect(skills?.id).toBe(scribe?.id);
        expect(skills?.startTime).not.toBe(scribe?.startTime);
    });

    test("readRunOverview is source-scoped too", async () => {
        const { readRunOverview } = await loadRuns();

        // RunOverview carries no task counts (they're per-task rows plus
        // aggregates), so assert on startedAt — the field that differs per
        // source in the fixtures and would be identical if both reads landed
        // in the same directory.
        expect((await readRunOverview(RUN_ID))?.startedAt).toBe(
            "2026-08-14T12:00:00Z",
        );
        expect((await readRunOverview(RUN_ID, SCRIBE_SOURCE))?.startedAt).toBe(
            "2026-08-14T18:00:00Z",
        );
    });

    test("a source with no such run reads null rather than the other source's", async () => {
        // The failure mode without per-source roots isn't just wrong numbers —
        // it's a run that doesn't exist in this container rendering anyway.
        const onlyInSkills = "2026-01-01_00-00-00";
        const dir = path.join(localDir, onlyInSkills);
        await fs.mkdir(dir, { recursive: true });
        await fs.writeFile(
            path.join(dir, "run.json"),
            runJson({
                tasksRun: 7,
                tasksSucceeded: 7,
                startTime: "2026-01-01T00:00:00Z",
            }),
        );

        const { readRunSummary } = await loadRuns();
        expect(await readRunSummary(onlyInSkills)).not.toBeNull();
        expect(await readRunSummary(onlyInSkills, SCRIBE_SOURCE)).toBeNull();
    });

    test("listRunIds lists each source's own tree in local mode", async () => {
        // Regression test for the shipped bug: listRunIdsRemote dropped its
        // container argument in local mode, so this returned the skills ids for
        // every source while the readers above resolved under the sibling dir.
        const scribeOnly = "2026-08-13_09-00-00";
        await fs.mkdir(path.join(`${localDir}-scribe`, scribeOnly), {
            recursive: true,
        });
        await fs.writeFile(
            path.join(`${localDir}-scribe`, scribeOnly, "run.json"),
            runJson({
                tasksRun: 1,
                tasksSucceeded: 1,
                startTime: "2026-08-13T09:00:00Z",
            }),
        );

        const { listRunIds } = await loadRuns();

        expect(await listRunIds()).toEqual([RUN_ID]);
        expect(await listRunIds(SCRIBE_SOURCE)).toEqual([RUN_ID, scribeOnly]);
    });

    test("latestRunId is per source", async () => {
        const { latestRunId } = await loadRuns();
        expect(await latestRunId()).toBe(RUN_ID);
        expect(await latestRunId(SCRIBE_SOURCE)).toBe(RUN_ID);

        // A newer run in only one source must not become the other's latest.
        const newer = "2026-08-15_00-00-00";
        await fs.mkdir(path.join(`${localDir}-scribe`, newer), {
            recursive: true,
        });
        await fs.writeFile(
            path.join(`${localDir}-scribe`, newer, "run.json"),
            runJson({
                tasksRun: 1,
                tasksSucceeded: 1,
                startTime: "2026-08-15T00:00:00Z",
            }),
        );

        const { latestRunId: fresh } = await loadRuns();
        expect(await fresh()).toBe(RUN_ID);
        expect(await fresh(SCRIBE_SOURCE)).toBe(newer);
    });
});
