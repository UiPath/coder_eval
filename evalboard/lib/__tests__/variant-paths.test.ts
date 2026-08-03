import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The variant-aware readers (readTaskDetail / readTaskReplicates / readLogTail /
// collectTaskFiles / resolveSafePath) all resolve a task's content under
// <run>/<variant>/<task>/<NN>/. RUNS_DIR is baked from EVALBOARD_LOCAL_RUNS_DIR
// at *import* time, so — like collect.test.ts — each test stubs the env to a
// throwaway runs dir and dynamically imports a fresh module copy so RUNS_DIR
// picks it up. LOCAL mode makes ensureTaskDir a no-op, so these exercise the
// pure on-disk path resolution deterministically.
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

const RUN = "2026-01-01_00-00-00";
const TASK = "demo-task";

// A multi-model (A/B) run: two variants ran the SAME task. They share the
// task_id at replicate 0 and differ only by variant_id / model_used — exactly
// the shape that used to collapse into one row and 404 on the detail page.
beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-variant-"));
    await write(
        `${RUN}/run.json`,
        JSON.stringify({
            run_id: "x",
            task_results: [
                {
                    task_id: TASK,
                    variant_id: "kimi-k3",
                    model_used: "moonshotai/kimi-k3",
                    replicate_index: 0,
                    status: "SUCCESS",
                },
                {
                    task_id: TASK,
                    variant_id: "glm-5-2",
                    model_used: "z-ai/glm-5.2",
                    replicate_index: 0,
                    status: "FAILURE",
                },
                // A second replicate of ONE variant, to prove replicate listing
                // is scoped to the selected variant.
                {
                    task_id: TASK,
                    variant_id: "kimi-k3",
                    model_used: "moonshotai/kimi-k3",
                    replicate_index: 1,
                    status: "SUCCESS",
                },
            ],
        }),
    );
    await write(`${RUN}/kimi-k3/${TASK}/00/task.json`, "{}");
    await write(`${RUN}/kimi-k3/${TASK}/00/task.log`, "kimi log");
    await write(`${RUN}/kimi-k3/${TASK}/01/task.json`, "{}");
    await write(`${RUN}/glm-5-2/${TASK}/00/task.json`, "{}");
    await write(`${RUN}/glm-5-2/${TASK}/00/task.log`, "glm log");
    await write(`${RUN}/glm-5-2/${TASK}/00/artifacts/out.txt`, "glm artifact");
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("readTaskDetail — variant selects the model's own row + content", () => {
    test("?v selects the matching model (not just the first variant)", async () => {
        const { readTaskDetail } = await loadRuns();
        const kimi = await readTaskDetail(RUN, TASK, 0, "kimi-k3");
        const glm = await readTaskDetail(RUN, TASK, 0, "glm-5-2");
        expect(kimi?.model).toBe("moonshotai/kimi-k3");
        expect(kimi?.variant).toBe("kimi-k3");
        expect(kimi?.status).toBe("SUCCESS");
        // The SECOND variant resolves to its OWN row — before the fix every
        // variant collapsed onto the first one at replicate 0.
        expect(glm?.model).toBe("z-ai/glm-5.2");
        expect(glm?.status).toBe("FAILURE");
    });

    test("a bare URL (no ?v) resolves the run's actual arm instead of 404ing", async () => {
        // The 404-regression fix: with no ?v and no "default" arm, readTaskDetail
        // resolves to the run's first arm (kimi-k3 here) and renders it, rather
        // than matching the literal "default" (zero rows → notFound). This is what
        // keeps pre-existing ?v-less deep links / bookmarks working.
        const { readTaskDetail } = await loadRuns();
        const task = await readTaskDetail(RUN, TASK, 0);
        expect(task).not.toBeNull();
        expect(task?.variant).toBe("kimi-k3");
    });

    test("an unsafe explicit variant is sanitized to 'default' (no path escape)", async () => {
        const { readTaskDetail } = await loadRuns();
        // "../glm-5-2" is not a valid id → sanitized to "default", which has no
        // row here → null. It must NOT traverse into the glm-5-2 subtree.
        expect(await readTaskDetail(RUN, TASK, 0, "../glm-5-2")).toBeNull();
        // A bare ".." is now rejected by isValidId too (was the traversal hole).
        expect(await readTaskDetail(RUN, TASK, 0, "..")).toBeNull();
    });
});

describe("legacy / single-config layout (no variant_id, <run>/default/)", () => {
    const LRUN = "2026-02-02_00-00-00";
    const LTASK = "legacy-task";

    async function loadLegacy() {
        // A run whose rows omit variant_id entirely, content under default/ —
        // the pre-variant on-disk shape the compat claim depends on.
        await write(
            `${LRUN}/run.json`,
            JSON.stringify({
                run_id: "x",
                task_results: [
                    { task_id: LTASK, replicate_index: 0, status: "SUCCESS" },
                ],
            }),
        );
        await write(`${LRUN}/default/${LTASK}/00/task.json`, "{}");
        await write(`${LRUN}/default/${LTASK}/00/task.log`, "legacy log");
        return loadRuns();
    }

    test("readTaskDetail with no ?v resolves the default/ row + content", async () => {
        const { readTaskDetail } = await loadLegacy();
        const task = await readTaskDetail(LRUN, LTASK, 0);
        expect(task).not.toBeNull();
        expect(task?.variant).toBeNull(); // legacy row carries no variant_id
        expect(task?.status).toBe("SUCCESS");
    });

    test("readTaskReplicates / readLogTail resolve the default/ subdir", async () => {
        const { readTaskReplicates, readLogTail } = await loadLegacy();
        expect(await readTaskReplicates(LRUN, LTASK)).toEqual([0]);
        expect(await readLogTail(LRUN, LTASK, 0)).toBe("legacy log");
    });
});

describe("readTaskReplicates — scoped to the selected variant", () => {
    test("lists only the chosen model's replicates", async () => {
        const { readTaskReplicates } = await loadRuns();
        expect(await readTaskReplicates(RUN, TASK, "kimi-k3")).toEqual([0, 1]);
        expect(await readTaskReplicates(RUN, TASK, "glm-5-2")).toEqual([0]);
    });
});

describe("readLogTail — reads the variant's log", () => {
    test("each variant's task.log is read from its own subdir", async () => {
        const { readLogTail } = await loadRuns();
        expect(await readLogTail(RUN, TASK, 0, "kimi-k3")).toBe("kimi log");
        expect(await readLogTail(RUN, TASK, 0, "glm-5-2")).toBe("glm log");
    });
});

describe("collectTaskFiles — zips the variant's folder", () => {
    test("collects files under the requested variant only", async () => {
        const { collectTaskFiles } = await loadRuns();
        const files = await collectTaskFiles(RUN, TASK, "glm-5-2");
        const rels = files?.map((f) => f.relPath).sort();
        expect(rels).toEqual(["00/artifacts/out.txt", "00/task.json", "00/task.log"]);
        for (const f of files ?? []) {
            expect(f.abs).toContain(path.join("glm-5-2", TASK));
        }
    });
});

describe("resolveSafePath — variant-prefixed artifact URLs", () => {
    test("resolves a non-default variant artifact path", async () => {
        const { resolveSafePath } = await loadRuns();
        const abs = await resolveSafePath(
            RUN,
            `glm-5-2/${TASK}/00/artifacts/out.txt`,
        );
        expect(abs).not.toBeNull();
        expect(abs).toContain(path.join(RUN, "glm-5-2", TASK));
    });

    test("rejects traversal outside the run dir (relative + absolute)", async () => {
        const { resolveSafePath } = await loadRuns();
        expect(await resolveSafePath(RUN, "../../etc/passwd")).toBeNull();
        expect(await resolveSafePath(RUN, "/etc/passwd")).toBeNull();
        // dot-only variant segment: isValidId now rejects "..", so the prefetch
        // falls through and the containment check nulls it.
        expect(await resolveSafePath(RUN, `../${TASK}/00/task.json`)).toBeNull();
    });
});

describe("dot-segment traversal is closed at the id guard", () => {
    test("collectTaskFiles rejects a '..' runId / variant (no exfil via download)", async () => {
        // Before the fix, isValidId admitted ".." so collectTaskFiles("..", …, "..")
        // enumerated a sibling dir. Now every dot segment is rejected.
        const { collectTaskFiles } = await loadRuns();
        // runId ".." → rejected outright.
        expect(await collectTaskFiles("..", "secret", "..")).toBeNull();
        // variant ".." → sanitized to "default" (a nonexistent subtree here), so
        // it resolves to nothing rather than escaping into "../<task>".
        expect(await collectTaskFiles(RUN, TASK, "..")).toBeNull();
    });

    test("isValidId rejects '.' and '..' but accepts real ids", async () => {
        const { isValidId } = await import("../blob");
        expect(isValidId("..")).toBe(false);
        expect(isValidId(".")).toBe(false);
        expect(isValidId("kimi-k3")).toBe(true);
        expect(isValidId("default")).toBe(true);
        expect(isValidId("gpt-5.6")).toBe(true); // dots inside a real id are fine
    });
});
