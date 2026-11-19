// @vitest-environment node
import { promises as fs, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const FIX = path.join(__dirname, "fixtures", "skill-doctor");
const RUN = "2099-01-03_04-23-30";

// Blob name (relative to the container) -> body; a missing name is a 404.
const state = vi.hoisted(() => ({
    blobs: {} as Record<string, string>,
    downloads: [] as string[],
    failWith: null as null | { statusCode: number },
}));

vi.mock("@azure/storage-blob", () => ({
    BlobServiceClient: class {
        getContainerClient() {
            return {
                getBlobClient: (name: string) => ({
                    downloadToFile: async (dest: string) => {
                        state.downloads.push(name);
                        if (state.failWith)
                            throw Object.assign(new Error("blob failed"), state.failWith);
                        if (!(name in state.blobs))
                            throw Object.assign(new Error("404"), { statusCode: 404 });
                        await fs.writeFile(dest, state.blobs[name]);
                    },
                }),
            };
        }
    },
}));
vi.mock("@azure/identity", () => ({ DefaultAzureCredential: class {} }));

const fixture = (p: string) => readFileSync(path.join(FIX, p), "utf-8");
const STATUS = fixture("status.json");

function seedBlobs(status: string = STATUS) {
    const d = `${RUN}/skill-doctor`;
    state.blobs = {
        [`${d}/status.json`]: status,
        [`${d}/triage-1000001.json`]: fixture("triage-1000001.json"),
        [`${d}/fixes-1000001/fixes.json`]: fixture("fixes-1000001/fixes.json"),
        [`${d}/fixes-1000001/example-grader-fix.patch`]: fixture(
            "fixes-1000001/example-grader-fix.patch",
        ),
    };
}

function statusWith(patch: Record<string, unknown>): string {
    return JSON.stringify({ ...JSON.parse(STATUS), ...patch });
}

let root: string;

beforeEach(async () => {
    vi.resetModules();
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", "");
    root = await fs.mkdtemp(path.join(os.tmpdir(), "doctor-blob-"));
    vi.stubEnv("EVALBOARD_RUNS_DIR", root);
    state.downloads = [];
    state.failWith = null;
    seedBlobs();
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(root, { recursive: true, force: true });
});

async function ensure() {
    const { ensureRunDoctor } = await import("../blob");
    return ensureRunDoctor("runs", RUN, root);
}

describe("ensureRunDoctor", () => {
    test("a done report: status, triage, fixes.json and the one patch", async () => {
        await ensure();
        expect(state.downloads.sort()).toEqual(
            [
                `${RUN}/skill-doctor/status.json`,
                `${RUN}/skill-doctor/triage-1000001.json`,
                `${RUN}/skill-doctor/fixes-1000001/fixes.json`,
                `${RUN}/skill-doctor/fixes-1000001/example-grader-fix.patch`,
            ].sort(),
        );
    });

    test("no report: one 404, swallowed", async () => {
        state.blobs = {};
        await expect(ensure()).resolves.toBeUndefined();
        expect(state.downloads).toEqual([`${RUN}/skill-doctor/status.json`]);
    });

    test("running: the triage is not fetched", async () => {
        seedBlobs(statusWith({ state: "running", fixes_dir: null }));
        await ensure();
        expect(state.downloads).toEqual([`${RUN}/skill-doctor/status.json`]);
    });

    test("a bad triage_file name is never fetched", async () => {
        seedBlobs(statusWith({ triage_file: "../run.json", fixes_dir: null }));
        await ensure();
        expect(state.downloads).toEqual([`${RUN}/skill-doctor/status.json`]);
    });

    test("schema_version 2: nothing beyond status.json", async () => {
        seedBlobs(statusWith({ schema_version: 2 }));
        await ensure();
        expect(state.downloads).toEqual([`${RUN}/skill-doctor/status.json`]);
    });

    test("status.json is refetched after a minute; the triage never", async () => {
        await ensure();
        const st = path.join(root, RUN, "skill-doctor", "status.json");
        const old = new Date(Date.now() - 120_000);
        await fs.utimes(st, old, old);
        state.downloads = [];
        await ensure();
        expect(state.downloads).toEqual([`${RUN}/skill-doctor/status.json`]);
    });
});

describe("readRunDoctor", () => {
    test("parses the cached report", async () => {
        const { readRunDoctor } = await import("../runs");
        const d = await readRunDoctor(RUN);
        expect(d?.status.state).toBe("done");
        expect(d?.triage?.tasks).toHaveLength(3);
        expect(d?.fixes?.candidates).toHaveLength(2);
    });

    test("never throws: a storage error is no report", async () => {
        state.failWith = { statusCode: 500 };
        const { readRunDoctor } = await import("../runs");
        const err = vi.spyOn(console, "error").mockImplementation(() => {});
        await expect(readRunDoctor(RUN)).resolves.toBeNull();
        err.mockRestore();
    });

    test("an unparsable status is no report", async () => {
        seedBlobs("{oops");
        const { readRunDoctor } = await import("../runs");
        await expect(readRunDoctor(RUN)).resolves.toBeNull();
    });

    test("the patch is served by /api/file's path check from the cache", async () => {
        const { readRunDoctor, resolveSafePath } = await import("../runs");
        await readRunDoctor(RUN);
        state.downloads = [];
        const abs = await resolveSafePath(
            RUN,
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        expect(abs).toBe(
            path.join(
                await fs.realpath(root),
                RUN,
                "skill-doctor",
                "fixes-1000001",
                "example-grader-fix.patch",
            ),
        );
        // No listing or run.json fetch for a doctor path.
        expect(state.downloads).toEqual([]);
        expect(await resolveSafePath(RUN, "skill-doctor/../../x")).toBeNull();
    });
});
