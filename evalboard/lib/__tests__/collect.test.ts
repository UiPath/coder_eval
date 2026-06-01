import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// collectTaskFiles / collectRunFiles read RUNS_DIR, which lib/blob.ts resolves
// from EVALBOARD_LOCAL_RUNS_DIR at *import* time. So each test stubs the env to
// a throwaway runs dir, then dynamically imports a fresh module copy
// (vi.resetModules) so RUNS_DIR picks up that path.
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

beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-collect-"));
    await write(`${RUN}/run.json`, '{"run_id":"x"}');
    await write(`${RUN}/analysis.md`, "# notes");
    await write(`${RUN}/default/${TASK}/00/task.json`, "{}");
    await write(`${RUN}/default/${TASK}/00/task.log`, "log line");
    await write(`${RUN}/default/${TASK}/00/artifacts/main.py`, "print(1)\n");
    // Noise that walkArtifacts must drop from either zip.
    await write(`${RUN}/default/${TASK}/00/.venv/lib/x.py`, "noise");
    await write(`${RUN}/default/${TASK}/00/artifacts/app.pyc`, "noise");
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("collectTaskFiles", () => {
    test("returns the task's files, minus scaffolding noise", async () => {
        const { collectTaskFiles } = await loadRuns();
        const files = await collectTaskFiles(RUN, TASK);
        const rels = files?.map((f) => f.relPath).sort();
        expect(rels).toEqual([
            "00/artifacts/main.py",
            "00/task.json",
            "00/task.log",
        ]);
        // abs paths point under the task dir and exist.
        for (const f of files ?? []) {
            expect(f.abs).toContain(`${TASK}`);
        }
    });

    test("rejects an invalid id", async () => {
        const { collectTaskFiles } = await loadRuns();
        expect(await collectTaskFiles(RUN, "../escape")).toBeNull();
    });

    test("returns null for a missing task", async () => {
        const { collectTaskFiles } = await loadRuns();
        expect(await collectTaskFiles(RUN, "nope")).toBeNull();
    });
});

describe("collectRunFiles", () => {
    test("returns run-level files + every task's files, minus noise", async () => {
        const { collectRunFiles } = await loadRuns();
        const files = await collectRunFiles(RUN);
        const rels = files?.map((f) => f.relPath).sort();
        expect(rels).toEqual([
            "analysis.md",
            `default/${TASK}/00/artifacts/main.py`,
            `default/${TASK}/00/task.json`,
            `default/${TASK}/00/task.log`,
            "run.json",
        ]);
    });

    test("returns null for a missing run", async () => {
        const { collectRunFiles } = await loadRuns();
        expect(await collectRunFiles("9999-99-99_00-00-00")).toBeNull();
    });
});
