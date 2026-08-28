import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Same harness as collect.test.ts: the readers resolve RUNS_DIR from
// EVALBOARD_LOCAL_RUNS_DIR at *import* time, so each test stubs the env to a
// throwaway runs dir and then dynamically imports a fresh module copy.
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

const TASK = "demo-task";

// A two-arm run, exactly as coder_eval's experiment layer lays it out:
// run.json rows stamped with variant_id, content under <run>/<variant>/<task>/<NN>/.
const AB_RUN = "2026-01-02_00-00-00";
// A run predating variants: no variant_id on the rows, content under
// <run>/default/<task>/<NN>/. This is the backward-compatibility fixture.
const LEGACY_RUN = "2026-01-01_00-00-00";

beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-variants-"));

    await write(
        `${AB_RUN}/run.json`,
        JSON.stringify({
            task_results: [
                {
                    task_id: TASK,
                    variant_id: "live-v1",
                    replicate_index: 0,
                    status: "SUCCESS",
                    weighted_score: 1,
                },
                {
                    task_id: TASK,
                    variant_id: "preview-v2",
                    replicate_index: 0,
                    status: "FAILURE",
                    weighted_score: 0,
                },
                // A second replicate in one arm only, so replicate enumeration
                // must not leak across arms.
                {
                    task_id: TASK,
                    variant_id: "preview-v2",
                    replicate_index: 1,
                    status: "SUCCESS",
                    weighted_score: 1,
                },
            ],
        }),
    );
    await write(
        `${AB_RUN}/live-v1/${TASK}/00/task.json`,
        JSON.stringify({ final_status: "SUCCESS" }),
    );
    await write(`${AB_RUN}/live-v1/${TASK}/00/task.log`, "log from live-v1");
    await write(
        `${AB_RUN}/preview-v2/${TASK}/00/task.json`,
        JSON.stringify({ final_status: "FAILURE" }),
    );
    await write(
        `${AB_RUN}/preview-v2/${TASK}/00/task.log`,
        "log from preview-v2",
    );
    await write(
        `${AB_RUN}/preview-v2/${TASK}/01/task.json`,
        JSON.stringify({ final_status: "SUCCESS" }),
    );
    await write(`${AB_RUN}/preview-v2/${TASK}/01/task.log`, "log from replicate 1");

    await write(
        `${LEGACY_RUN}/run.json`,
        JSON.stringify({
            task_results: [{ task_id: TASK, status: "SUCCESS", weighted_score: 1 }],
        }),
    );
    await write(
        `${LEGACY_RUN}/default/${TASK}/00/task.json`,
        JSON.stringify({ final_status: "SUCCESS" }),
    );
    await write(`${LEGACY_RUN}/default/${TASK}/00/task.log`, "legacy log");
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("backward compatibility", () => {
    // The hard requirement: a run with no variant_id anywhere must resolve
    // exactly as it did before, with no caller passing a variant.
    test("a legacy run resolves with no variant argument", async () => {
        const { readTaskDetail, readLogTail, readTaskReplicates } =
            await loadRuns();
        const detail = await readTaskDetail(LEGACY_RUN, TASK);
        expect(detail?.status).toBe("SUCCESS");
        expect(await readLogTail(LEGACY_RUN, TASK)).toBe("legacy log");
        expect(await readTaskReplicates(LEGACY_RUN, TASK)).toEqual([0]);
    });

    test("a legacy row carries a null variantId, not a fabricated one", async () => {
        const { toTaskRow } = await loadRuns();
        expect(toTaskRow({ task_id: TASK }).variantId).toBeNull();
    });
});

describe("multi-variant reads", () => {
    // Without variant-aware row matching both arms resolve to the first matching
    // row, so the failing arm would render the passing arm's result.
    test("each arm resolves to its own row", async () => {
        const { readTaskDetail } = await loadRuns();
        const a = await readTaskDetail(AB_RUN, TASK, 0, undefined, "live-v1");
        const b = await readTaskDetail(AB_RUN, TASK, 0, undefined, "preview-v2");
        expect(a?.status).toBe("SUCCESS");
        expect(b?.status).toBe("FAILURE");
        expect(a?.variantId).toBe("live-v1");
        expect(b?.variantId).toBe("preview-v2");
    });

    // And its own content: the row is only half the resolution, the path is the
    // other half.
    test("each arm resolves to its own content directory", async () => {
        const { readLogTail } = await loadRuns();
        expect(
            await readLogTail(AB_RUN, TASK, 0, undefined, undefined, "live-v1"),
        ).toBe("log from live-v1");
        expect(
            await readLogTail(
                AB_RUN,
                TASK,
                0,
                undefined,
                undefined,
                "preview-v2",
            ),
        ).toBe("log from preview-v2");
    });

    test("replicate enumeration is scoped to the arm", async () => {
        const { readTaskReplicates } = await loadRuns();
        expect(
            await readTaskReplicates(AB_RUN, TASK, undefined, "live-v1"),
        ).toEqual([0]);
        expect(
            await readTaskReplicates(AB_RUN, TASK, undefined, "preview-v2"),
        ).toEqual([0, 1]);
    });

    test("a replicate within an arm resolves independently", async () => {
        const { readLogTail } = await loadRuns();
        expect(
            await readLogTail(
                AB_RUN,
                TASK,
                1,
                undefined,
                undefined,
                "preview-v2",
            ),
        ).toBe("log from replicate 1");
    });

    // An arm that isn't in the run must 404 rather than silently fall back to
    // another arm's result.
    test("an unknown arm yields no detail", async () => {
        const { readTaskDetail } = await loadRuns();
        expect(
            await readTaskDetail(AB_RUN, TASK, 0, undefined, "no-such-arm"),
        ).toBeNull();
    });

    test("the default arm is absent from a run whose arms are both named", async () => {
        const { readTaskDetail } = await loadRuns();
        expect(await readTaskDetail(AB_RUN, TASK)).toBeNull();
    });
});

describe("collectTaskFiles under variants", () => {
    test("zips the named arm's folder", async () => {
        const { collectTaskFiles } = await loadRuns();
        const files = await collectTaskFiles(
            AB_RUN,
            TASK,
            undefined,
            "preview-v2",
        );
        expect(files?.map((f) => f.relPath).sort()).toEqual([
            "00/task.json",
            "00/task.log",
            "01/task.json",
            "01/task.log",
        ]);
    });

    test("rejects a variant id that could escape the run dir", async () => {
        const { collectTaskFiles } = await loadRuns();
        expect(
            await collectTaskFiles(AB_RUN, TASK, undefined, ".."),
        ).toBeNull();
    });
});
