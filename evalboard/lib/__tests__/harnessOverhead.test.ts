import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// End-to-end: the two turn-level timing buckets survive the trip from
// task.json's `iterations` onto TaskDetail. `sumHarnessOverhead` is unit-tested
// in runs.test.ts; what only a read off disk can catch is a misspelled raw key,
// since every TurnEntry field is optional and a typo would just parse as
// absent. Mirrors providerCalls.test.ts's env-stub + fresh-import pattern.
const RUN = "2026-01-01_00-00-00";
const TASK = "demo-task";
let tmp: string;

async function write(rel: string, body: string): Promise<void> {
    const abs = path.join(tmp, rel);
    await fs.mkdir(path.dirname(abs), { recursive: true });
    await fs.writeFile(abs, body);
}

async function loadRuns() {
    vi.resetModules();
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", tmp);
    return import("../runs");
}

async function writeTask(iterations: unknown[]): Promise<void> {
    await write(
        `${RUN}/run.json`,
        JSON.stringify({
            run_id: RUN,
            task_results: [{ task_id: TASK, status: "success" }],
        }),
    );
    await write(
        `${RUN}/default/${TASK}/00/task.json`,
        JSON.stringify({ final_status: "success", iterations }),
    );
}

beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-overhead-"));
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("readTaskDetail: harness startup/teardown", () => {
    test("sums both buckets across the task's turns", async () => {
        await writeTask([
            { harness_startup_ms: 3047.9, harness_teardown_ms: 33.1 },
            { harness_startup_ms: 120.5, harness_teardown_ms: 4.2 },
        ]);
        const { readTaskDetail } = await loadRuns();
        const detail = await readTaskDetail(RUN, TASK);
        expect(detail?.harnessStartupMs).toBeCloseTo(3168.4, 3);
        expect(detail?.harnessTeardownMs).toBeCloseTo(37.3, 3);
    });

    test("an older run without the fields reports null, not zero", async () => {
        await writeTask([{ model_used: "claude-haiku-4-5" }]);
        const { readTaskDetail } = await loadRuns();
        const detail = await readTaskDetail(RUN, TASK);
        expect(detail?.harnessStartupMs).toBeNull();
        expect(detail?.harnessTeardownMs).toBeNull();
    });

    test("a measured zero head is preserved as 0", async () => {
        // An in-process SDK's first generation window already covers dispatch,
        // so 0.0 is its honest answer and must not read as "never measured".
        await writeTask([{ harness_startup_ms: 0.0, harness_teardown_ms: 834.7 }]);
        const { readTaskDetail } = await loadRuns();
        const detail = await readTaskDetail(RUN, TASK);
        expect(detail?.harnessStartupMs).toBe(0);
        expect(detail?.harnessTeardownMs).toBeCloseTo(834.7, 3);
    });
});
