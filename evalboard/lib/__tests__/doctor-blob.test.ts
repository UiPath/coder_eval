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
    // Fail only this blob name (with failWith's status).
    failOn: null as null | string,
}));

vi.mock("@azure/storage-blob", () => ({
    BlobServiceClient: class {
        getContainerClient() {
            return {
                getBlobClient: (name: string) => ({
                    downloadToFile: async (dest: string) => {
                        state.downloads.push(name);
                        if (state.failWith && (state.failOn === null || state.failOn === name))
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
    vi.stubEnv("EVALBOARD_EDITION", "internal");
    state.downloads = [];
    state.failWith = null;
    state.failOn = null;
    seedBlobs();
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(root, { recursive: true, force: true });
});

async function read() {
    const { readRunDoctor } = await import("../runs");
    return readRunDoctor(RUN);
}

const D = `${RUN}/skill-doctor`;

describe("ensureRunDoctorFile guard", () => {
    test.each([
        ["../x"],
        ["fixes-1/../status.json"],
        ["a/b/c"],
        [""],
        ["/abs.json"],
        ["a b.json"],
    ])("rejects %j before any download", async (rel) => {
        const { ensureRunDoctorFile } = await import("../blob");
        await expect(ensureRunDoctorFile("runs", RUN, rel, root)).rejects.toThrow(
            /Invalid skill-doctor path/,
        );
        expect(state.downloads).toEqual([]);
    });

    test("rejects a bad run id before any download", async () => {
        const { ensureRunDoctorFile } = await import("../blob");
        await expect(ensureRunDoctorFile("runs", "../x", "status.json", root)).rejects.toThrow(
            /Invalid runId/,
        );
        expect(state.downloads).toEqual([]);
    });
});

describe("readRunDoctor: what it fetches", () => {
    test("a done report: status, triage, fixes.json and the one patch", async () => {
        await read();
        expect(state.downloads.sort()).toEqual(
            [
                `${D}/status.json`,
                `${D}/triage-1000001.json`,
                `${D}/fixes-1000001/fixes.json`,
                `${D}/fixes-1000001/example-grader-fix.patch`,
            ].sort(),
        );
    });

    test("no report: one 404, swallowed", async () => {
        state.blobs = {};
        await expect(read()).resolves.toBeNull();
        expect(state.downloads).toEqual([`${D}/status.json`]);
    });

    test("running: the triage is not fetched", async () => {
        seedBlobs(statusWith({ state: "running", fixes_dir: null }));
        expect((await read())?.status.state).toBe("running");
        expect(state.downloads).toEqual([`${D}/status.json`]);
    });

    test("a bad triage_file name is never fetched", async () => {
        seedBlobs(statusWith({ triage_file: "../run.json", fixes_dir: null }));
        expect((await read())?.triage).toBeNull();
        expect(state.downloads).toEqual([`${D}/status.json`]);
    });

    test("schema_version 2: nothing beyond status.json, and a warning", async () => {
        seedBlobs(statusWith({ schema_version: 2 }));
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        await expect(read()).resolves.toBeNull();
        expect(state.downloads).toEqual([`${D}/status.json`]);
        expect(warn).toHaveBeenCalledTimes(1);
        // Names the file, never its contents.
        expect(String(warn.mock.calls[0][0])).toContain("status.json");
        expect(String(warn.mock.calls[0][0])).not.toContain("schema_version");
        warn.mockRestore();
    });

    test("status.json is refetched after a minute, not within it; the rest never", async () => {
        await read();
        state.downloads = [];
        await read();
        expect(state.downloads).toEqual([]);
        const st = path.join(root, RUN, "skill-doctor", "status.json");
        const old = new Date(Date.now() - 120_000);
        await fs.utimes(st, old, old);
        await read();
        expect(state.downloads).toEqual([`${D}/status.json`]);
    });

    test("patch fan-out is capped and ids are checked", async () => {
        const doc = JSON.parse(fixture("fixes-1000001/fixes.json"));
        const base = doc.candidates[0];
        doc.candidates = [
            { ...base, id: "../escape" },
            ...Array.from({ length: 15 }, (_, i) => ({ ...base, id: `fix-${i}` })),
        ];
        state.blobs[`${D}/fixes-1000001/fixes.json`] = JSON.stringify(doc);
        await read();
        const patches = state.downloads.filter((n) => n.endsWith(".patch"));
        expect(patches).toHaveLength(10);
        expect(patches.some((n) => n.includes(".."))).toBe(false);
    });
});

describe("readRunDoctor", () => {
    test("parses the cached report", async () => {
        const { readRunDoctor } = await import("../runs");
        const d = await readRunDoctor(RUN);
        expect(d?.status.state).toBe("done");
        expect(d?.triage?.tasks).toHaveLength(5);
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

    test("/api/file's path check serves a cached patch without a listing", async () => {
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
        expect(state.downloads).toEqual([]);
    });

    test("a cold cache: /api/file loads status + fixes.json, then the listed patch", async () => {
        const { resolveSafePath } = await import("../runs");
        const abs = await resolveSafePath(
            RUN,
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        expect(state.downloads).toContain(`${D}/status.json`);
        expect(state.downloads).toContain(`${D}/fixes-1000001/fixes.json`);
        expect(state.downloads).toContain(`${D}/fixes-1000001/example-grader-fix.patch`);
        expect(await fs.readFile(abs!, "utf-8")).toContain("diff --git");
    });

    test("a patch fixes.json does not list, or another fixes dir, is refused", async () => {
        const { resolveSafePath } = await import("../runs");
        expect(await resolveSafePath(RUN, "skill-doctor/fixes-1000001/made-up.patch")).toBeNull();
        expect(
            await resolveSafePath(RUN, "skill-doctor/fixes-9/example-grader-fix.patch"),
        ).toBeNull();
        expect(state.downloads.some((n) => n.endsWith("made-up.patch"))).toBe(false);
        expect(state.downloads.some((n) => n.includes("fixes-9"))).toBe(false);
    });

    test("kill switch off or the OSS edition: nothing served, nothing fetched", async () => {
        vi.stubEnv("EVALBOARD_TASK_ANALYSIS", "Off");
        let { resolveSafePath } = await import("../runs");
        const rel = "skill-doctor/fixes-1000001/example-grader-fix.patch";
        expect(await resolveSafePath(RUN, rel)).toBeNull();
        vi.resetModules();
        vi.stubEnv("EVALBOARD_TASK_ANALYSIS", "");
        vi.stubEnv("EVALBOARD_EDITION", "");
        ({ resolveSafePath } = await import("../runs"));
        expect(await resolveSafePath(RUN, rel)).toBeNull();
        expect(state.downloads).toEqual([]);
    });

    test("a storage error on the patch: no throw, nothing on disk", async () => {
        state.failWith = { statusCode: 500 };
        state.failOn = `${D}/fixes-1000001/example-grader-fix.patch`;
        const err = vi.spyOn(console, "error").mockImplementation(() => {});
        const { resolveSafePath } = await import("../runs");
        const abs = await resolveSafePath(
            RUN,
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        // /api/file then answers 404 for the missing file.
        expect(abs).not.toBeNull();
        await expect(fs.access(abs!)).rejects.toThrow();
        expect(err).toHaveBeenCalled();
        err.mockRestore();
    });

    test("only patch paths are served from skill-doctor/", async () => {
        const { resolveSafePath } = await import("../runs");
        for (const bad of [
            "skill-doctor/status.json",
            "skill-doctor/triage-1000001.json",
            "skill-doctor/fixes-1000001/fixes.json",
            "skill-doctor/fixes-1000001/../status.json",
            "skill-doctor/other/x.patch",
            "skill-doctor/fixes-1000001/a/b.patch",
            "skill-doctor/../../x",
        ]) {
            expect(await resolveSafePath(RUN, bad)).toBeNull();
        }
        expect(state.downloads).toEqual([]);
    });
});
