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
        [`${d}/fixes-1000001/opened.json`]: JSON.stringify({ opened: [] }),
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
                `${D}/fixes-1000001/opened.json`,
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

    test.each(["queued", "running", "failed", "skipped", "retracted"])(
        "%s with fixes_dir kept: only status.json is fetched, no patch is served",
        async (st) => {
            seedBlobs(statusWith({ state: st }));
            const doctor = await read();
            expect(doctor?.status.state).toBe(st);
            expect(doctor?.triage ?? null).toBeNull();
            expect(doctor?.fixes ?? null).toBeNull();
            const { resolveSafePath } = await import("../runs");
            expect(
                await resolveSafePath(RUN, "skill-doctor/fixes-1000001/example-grader-fix.patch"),
            ).toBeNull();
            expect([...new Set(state.downloads)]).toEqual([`${D}/status.json`]);
        },
    );

    test("a storage error on the triage: the page still loads, flagged fetchFailed", async () => {
        state.failWith = { statusCode: 500 };
        state.failOn = `${D}/triage-1000001.json`;
        const err = vi.spyOn(console, "error").mockImplementation(() => {});
        const doctor = await read();
        expect(doctor?.triage).toBeNull();
        expect(doctor?.fetchFailed).toBe(true);
        expect(err).toHaveBeenCalled();
        err.mockRestore();
    });

    test("a triage that names another run is ignored", async () => {
        const t = JSON.parse(fixture("triage-1000001.json"));
        t.run.run_id = "2099-01-02_04-23-30";
        state.blobs[`${D}/triage-1000001.json`] = JSON.stringify(t);
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const doctor = await read();
        expect(doctor?.triage).toBeNull();
        expect(doctor?.fetchFailed).toBe(false);
        expect(warn).toHaveBeenCalledWith(expect.stringMatching(/names run/));
        warn.mockRestore();
    });

    test("a fixes.json that names another run lists no patch", async () => {
        const f = JSON.parse(fixture("fixes-1000001/fixes.json"));
        f.run_id = "2099-01-02_04-23-30";
        state.blobs[`${D}/fixes-1000001/fixes.json`] = JSON.stringify(f);
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const doctor = await read();
        expect(doctor?.fixes).toBeNull();
        expect(doctor?.triage).not.toBeNull();
        const { resolveSafePath } = await import("../runs");
        expect(
            await resolveSafePath(RUN, "skill-doctor/fixes-1000001/example-grader-fix.patch"),
        ).toBeNull();
        expect(state.downloads.some((n) => n.endsWith(".patch"))).toBe(false);
        warn.mockRestore();
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

    test("status.json and opened.json are refetched after a minute, not within it; the rest never", async () => {
        await read();
        state.downloads = [];
        await read();
        expect(state.downloads).toEqual([]);
        const old = new Date(Date.now() - 120_000);
        for (const f of ["status.json", "fixes-1000001/opened.json"]) {
            await fs.utimes(path.join(root, RUN, "skill-doctor", f), old, old);
        }
        await read();
        expect(state.downloads.sort()).toEqual(
            [`${D}/status.json`, `${D}/fixes-1000001/opened.json`].sort(),
        );
    });

    test("an opened.json that changes is picked up after the refresh age", async () => {
        expect((await read())!.opened).toEqual({});
        state.blobs[`${D}/fixes-1000001/opened.json`] = JSON.stringify({
            opened: [{ task_id: "example-t1", pr_url: "https://github.com/UiPath/skills/pull/300" }],
        });
        // Within the minute: the cached copy.
        expect((await read())!.opened).toEqual({});
        const old = new Date(Date.now() - 120_000);
        await fs.utimes(path.join(root, RUN, "skill-doctor", "fixes-1000001", "opened.json"), old, old);
        expect((await read())!.opened).toEqual({
            "example-t1": "https://github.com/UiPath/skills/pull/300",
        });
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
        expect(d!.opened).toEqual({});
    });

    test("opened.json maps opened PRs by task; a bad host or a problem entry does not", async () => {
        state.blobs[`${D}/fixes-1000001/opened.json`] = JSON.stringify({
            opened: [
                { task_id: "example-t1", pr_url: "https://github.com/UiPath/skills/pull/300" },
                { task_id: "example-t2", pr_url: "https://evil.example/UiPath/skills/pull/301" },
                { task_id: "example-e1", problem: "push refused" },
            ],
        });
        const { readRunDoctor } = await import("../runs");
        const d = await readRunDoctor(RUN);
        expect(d!.opened).toEqual({ "example-t1": "https://github.com/UiPath/skills/pull/300" });
    });

    test("no or malformed opened.json: no opened PRs, no throw", async () => {
        delete state.blobs[`${D}/fixes-1000001/opened.json`];
        const { readRunDoctor } = await import("../runs");
        expect((await readRunDoctor(RUN))!.opened).toEqual({});
        vi.resetModules();
        state.blobs[`${D}/fixes-1000001/opened.json`] = "{not json";
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const { readRunDoctor: again } = await import("../runs");
        // Cached copy from before is fresh, so drop it to read the bad one.
        await fs.rm(path.join(root, RUN, "skill-doctor", "fixes-1000001", "opened.json"), { force: true });
        expect((await again(RUN))!.opened).toEqual({});
        expect(warn).toHaveBeenCalled();
        warn.mockRestore();
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

    test.each([
        ["the patch", `${D}/fixes-1000001/example-grader-fix.patch`],
        ["fixes.json", `${D}/fixes-1000001/fixes.json`],
        ["status.json", `${D}/status.json`],
    ])("a storage error on %s propagates (the route answers 5xx, not 403/404)", async (_, name) => {
        state.failWith = { statusCode: 500 };
        state.failOn = name;
        const { resolveSafePath } = await import("../runs");
        await expect(
            resolveSafePath(RUN, "skill-doctor/fixes-1000001/example-grader-fix.patch"),
        ).rejects.toThrow(/blob failed/);
    });

    test("a patch link fetches only that patch: a storage error elsewhere does not break it", async () => {
        state.failWith = { statusCode: 500 };
        state.failOn = `${D}/fixes-1000001/opened.json`;
        const { resolveSafePath } = await import("../runs");
        const abs = await resolveSafePath(
            RUN,
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        expect(abs).toMatch(/example-grader-fix\.patch$/);
        await expect(fs.access(abs!)).resolves.toBeUndefined();
        expect(state.downloads).not.toContain(`${D}/fixes-1000001/opened.json`);
    });

    test("normalized before the dispatch: ./ and ../ cannot skip the gate", async () => {
        vi.stubEnv("EVALBOARD_EDITION", "");
        const { resolveSafePath } = await import("../runs");
        for (const bad of [
            "./skill-doctor/fixes-1000001/example-grader-fix.patch",
            "activation/../skill-doctor/fixes-1000001/example-grader-fix.patch",
            "./skill-doctor/status.json",
            "../x",
            "..",
        ]) {
            expect(await resolveSafePath(RUN, bad)).toBeNull();
        }
        expect(state.downloads).toEqual([]);
    });

    test.each(["..", "."])("a run id of %j is refused", async (id) => {
        const { resolveSafePath } = await import("../runs");
        expect(await resolveSafePath(id, ".env")).toBeNull();
        expect(await resolveSafePath(id, "run.json")).toBeNull();
        expect(state.downloads).toEqual([]);
    });

    test("only patch paths are served from skill-doctor/", async () => {
        const { resolveSafePath } = await import("../runs");
        for (const bad of [
            "skill-doctor/status.json",
            "skill-doctor/triage-1000001.json",
            "skill-doctor/fixes-1000001/fixes.json",
            // /api/file serves .patch files only.
            "skill-doctor/fixes-1000001/example-grader-fix.md",
            "skill-doctor/fixes-1000001/opened.json",
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
