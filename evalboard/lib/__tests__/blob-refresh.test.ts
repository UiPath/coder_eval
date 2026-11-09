// @vitest-environment node
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const state = vi.hoisted(() => ({
    downloads: [] as string[],
    body: "{}",
    failWith: null as null | { statusCode: number },
}));

vi.mock("@azure/storage-blob", () => ({
    BlobServiceClient: class {
        getContainerClient() {
            return {
                getBlobClient: (name: string) => ({
                    downloadToFile: async (dest: string) => {
                        state.downloads.push(name);
                        if (state.failWith) {
                            throw Object.assign(new Error("blob failed"), state.failWith);
                        }
                        await fs.writeFile(dest, state.body);
                    },
                }),
            };
        }
    },
}));
vi.mock("@azure/identity", () => ({ DefaultAzureCredential: class {} }));

const RUN = "adhoc-2026-11-09_16-18-40";

let root: string;

async function ensureMeta(refreshAfterMs?: number) {
    const { ensureRunMeta } = await import("../blob");
    return ensureRunMeta("runs", RUN, root, refreshAfterMs);
}

async function localMeta(): Promise<string | null> {
    return fs.readFile(path.join(root, RUN, "meta.json"), "utf-8").catch(() => null);
}

async function seedLocal(body: string, ageMs: number) {
    const p = path.join(root, RUN, "meta.json");
    await fs.mkdir(path.dirname(p), { recursive: true });
    await fs.writeFile(p, body);
    const t = new Date(Date.now() - ageMs);
    await fs.utimes(p, t, t);
}

beforeEach(async () => {
    vi.resetModules();
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", "");
    root = await fs.mkdtemp(path.join(os.tmpdir(), "blob-refresh-"));
    state.downloads = [];
    state.body = '{"adhoc":true,"series":"fresh"}';
    state.failWith = null;
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(root, { recursive: true, force: true });
});

describe("ensureRunMeta", () => {
    test("downloads a meta.json that is not on disk", async () => {
        await ensureMeta();
        expect(state.downloads).toEqual([`${RUN}/meta.json`]);
        expect(await localMeta()).toBe(state.body);
    });

    test("never refetches a cached copy when no refresh age is given", async () => {
        await seedLocal("old", 10 * 86_400_000);
        await ensureMeta();
        expect(state.downloads).toEqual([]);
        expect(await localMeta()).toBe("old");
    });

    test("leaves a copy younger than the refresh age alone", async () => {
        await seedLocal("old", 5_000);
        await ensureMeta(60_000);
        expect(state.downloads).toEqual([]);
        expect(await localMeta()).toBe("old");
    });

    test("replaces a copy older than the refresh age with the blob's", async () => {
        await seedLocal('{"adhoc":true}', 120_000);
        await ensureMeta(60_000);
        expect(state.downloads).toEqual([`${RUN}/meta.json`]);
        expect(await localMeta()).toBe(state.body);
    });

    test("keeps the stale copy when the refetch fails", async () => {
        await seedLocal("old", 120_000);
        state.failWith = { statusCode: 503 };
        await expect(ensureMeta(60_000)).resolves.toBeUndefined();
        expect(await localMeta()).toBe("old");
    });

    test("an absent meta.json is not an error, any other failure still is", async () => {
        state.failWith = { statusCode: 404 };
        await expect(ensureMeta()).resolves.toBeUndefined();
        expect(await localMeta()).toBeNull();
        vi.resetModules();
        state.failWith = { statusCode: 503 };
        await expect(ensureMeta()).rejects.toThrow("blob failed");
    });
});

describe("ensureRunExperiment", () => {
    test("downloads experiment.json once and then serves the cached copy", async () => {
        const { ensureRunExperiment } = await import("../blob");
        await ensureRunExperiment("runs", RUN, root);
        await ensureRunExperiment("runs", RUN, root);
        expect(state.downloads).toEqual([`${RUN}/experiment.json`]);
    });
});
