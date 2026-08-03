import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// collectTaskFiles reads RUNS_DIR, resolved from EVALBOARD_LOCAL_RUNS_DIR at
// import time — so stub the env to a throwaway runs dir and import a fresh
// module copy (like collect.test.ts / the refresh route test).
let tmp: string;

async function write(rel: string, body: string): Promise<void> {
    const abs = path.join(tmp, rel);
    await fs.mkdir(path.dirname(abs), { recursive: true });
    await fs.writeFile(abs, body);
}

async function loadGet() {
    vi.resetModules();
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", tmp);
    return (await import("../route")).GET;
}

function get(qs: string): Request {
    return new Request(`http://test/api/download?${qs}`, { method: "GET" });
}

const RUN = "2026-01-01_00-00-00";

beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-download-"));
    // A/B task: only under the glm-5-2 arm (no default/ subtree).
    await write(`${RUN}/glm-5-2/ab-task/00/task.json`, "{}");
    await write(`${RUN}/glm-5-2/ab-task/00/artifacts/out.txt`, "glm out");
    // Single-config task: under default/.
    await write(`${RUN}/default/solo-task/00/task.json`, "{}");
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("GET /api/download — variant (?v=) wiring", () => {
    test("zips the requested arm's subtree", async () => {
        const GET = await loadGet();
        const res = await GET(get(`run=${RUN}&task=ab-task&v=glm-5-2`));
        expect(res.status).toBe(200);
        expect(res.headers.get("Content-Type")).toBe("application/zip");
        // Non-empty archive → the arm's files were found. (Dropping the variant
        // arg in the route would look in default/ab-task, which doesn't exist,
        // and 404 — this assertion kills that mutation.)
        expect(Number(res.headers.get("Content-Length"))).toBeGreaterThan(0);
    });

    test("a single-config task downloads with no ?v (default arm)", async () => {
        const GET = await loadGet();
        const res = await GET(get(`run=${RUN}&task=solo-task`));
        expect(res.status).toBe(200);
        expect(res.headers.get("Content-Type")).toBe("application/zip");
    });

    test("an unknown arm 404s rather than zipping the wrong subtree", async () => {
        const GET = await loadGet();
        const res = await GET(get(`run=${RUN}&task=ab-task&v=nope`));
        expect(res.status).toBe(404);
    });

    test("missing run -> 400", async () => {
        const GET = await loadGet();
        const res = await GET(get(`task=ab-task`));
        expect(res.status).toBe(400);
    });
});
