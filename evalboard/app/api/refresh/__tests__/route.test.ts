import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import {
    afterEach,
    beforeEach,
    describe,
    expect,
    test,
    vi,
} from "vitest";

// RUNS_DIR and LOCAL_RUNS_DIR are module-level consts read from env at import
// time, so each scenario sets env, resets the module registry, then imports a
// fresh copy of the route.
async function loadPost() {
    vi.resetModules();
    return (await import("../route")).POST;
}

function post(runId?: string): Request {
    const base = "http://test/api/refresh";
    const url =
        runId === undefined
            ? base
            : `${base}?run=${encodeURIComponent(runId)}`;
    return new Request(url, { method: "POST" });
}

const ENV_KEYS = ["EVALBOARD_LOCAL_RUNS_DIR", "EVALBOARD_RUNS_DIR"] as const;
let savedEnv: Record<string, string | undefined>;

beforeEach(() => {
    savedEnv = {};
    for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
});
afterEach(() => {
    for (const k of ENV_KEYS) {
        if (savedEnv[k] === undefined) delete process.env[k];
        else process.env[k] = savedEnv[k];
    }
});

describe("POST /api/refresh", () => {
    test("local mode: refuses with 400 and never deletes (data guard)", async () => {
        // In local mode RUNS_DIR is the real coder_eval runs dir (the source of
        // truth, not a cache). The guard must short-circuit before
        // clearRunCacheDir so a refresh can never rm real run data. This is the
        // single branch standing between "evict a cache" and "delete run data".
        const localDir = await fs.mkdtemp(
            path.join(os.tmpdir(), "refresh-local-"),
        );
        const sentinel = path.join(localDir, "some-run", "run.json");
        await fs.mkdir(path.dirname(sentinel), { recursive: true });
        await fs.writeFile(sentinel, "{}\n");

        process.env.EVALBOARD_LOCAL_RUNS_DIR = localDir;
        delete process.env.EVALBOARD_RUNS_DIR;

        const POST = await loadPost();
        const res = await POST(post("some-run"));

        expect(res.status).toBe(400);
        expect((await res.json()).error).toMatch(/local mode/);
        // Real run data is untouched.
        await expect(fs.access(sentinel)).resolves.toBeUndefined();

        await fs.rm(localDir, { recursive: true, force: true });
    });

    describe("blob (non-local) mode", () => {
        let cache: string;
        beforeEach(async () => {
            cache = await fs.mkdtemp(path.join(os.tmpdir(), "refresh-cache-"));
            delete process.env.EVALBOARD_LOCAL_RUNS_DIR;
            process.env.EVALBOARD_RUNS_DIR = cache;
        });
        afterEach(async () => {
            await fs.rm(cache, { recursive: true, force: true });
        });

        test("missing run param -> 400", async () => {
            const POST = await loadPost();
            const res = await POST(post());
            expect(res.status).toBe(400);
            expect((await res.json()).error).toMatch(/missing run/);
        });

        test('traversal id ".." -> 400, cache root untouched', async () => {
            // ".." passes isValidId (dots are word-ish) but clearRunCacheDir's
            // strict-child check rejects it, so the route returns 400 and the
            // cache root is never the rm target.
            const marker = path.join(cache, "keep.txt");
            await fs.writeFile(marker, "x");

            const POST = await loadPost();
            const res = await POST(post(".."));

            expect(res.status).toBe(400);
            expect((await res.json()).error).toMatch(/invalid run/);
            await expect(fs.access(marker)).resolves.toBeUndefined();
            await expect(fs.access(cache)).resolves.toBeUndefined();
        });

        test("valid run -> 204 and the cached dir is evicted", async () => {
            const runDir = path.join(cache, "2026-06-01_04-04-22");
            await fs.mkdir(runDir, { recursive: true });
            await fs.writeFile(path.join(runDir, "meta.json"), "{}\n");

            const POST = await loadPost();
            const res = await POST(post("2026-06-01_04-04-22"));

            expect(res.status).toBe(204);
            await expect(fs.access(runDir)).rejects.toThrow();
        });
    });
});
