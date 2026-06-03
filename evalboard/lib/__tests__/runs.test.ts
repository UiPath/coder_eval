import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import {
    afterAll,
    afterEach,
    beforeAll,
    beforeEach,
    describe,
    expect,
    test,
} from "vitest";
import {
    type ArtifactRef,
    clearRunCacheDir,
    isExcludedArtifact,
    sortArtifacts,
    toTaskRow,
    visibleTurnsFromRaw,
    walkArtifacts,
} from "../runs";

describe("toTaskRow", () => {
    test("propagates total_turns and expected_turns", () => {
        const row = toTaskRow({
            task_id: "x",
            total_turns: 7,
            expected_turns: 5,
        });
        expect(row.totalTurns).toBe(7);
        expect(row.expectedTurns).toBe(5);
    });

    test("legacy raw shape (no new fields) yields null", () => {
        const row = toTaskRow({ task_id: "x" });
        expect(row.totalTurns).toBeNull();
        expect(row.expectedTurns).toBeNull();
    });

    test("expected_turns explicitly null on raw yields null", () => {
        const row = toTaskRow({ task_id: "x", expected_turns: null });
        expect(row.expectedTurns).toBeNull();
    });
});

describe("isExcludedArtifact", () => {
    test("hides build artifacts, local state, and secrets", () => {
        for (const rel of [
            "t/artifacts/.venv/lib/x.py",
            "t/artifacts/node_modules/pkg/i.js",
            "t/artifacts/proj/bin/a.dll",
            "t/artifacts/__pycache__/m.pyc",
            "t/artifacts/a.pyc",
            "t/artifacts/uv.lock",
            "t/artifacts/state.db",
            "t/artifacts/state.db-wal",
            "t/artifacts/.env",
            "t/artifacts/config.env",
        ]) {
            expect(isExcludedArtifact(rel), rel).toBe(true);
        }
    });

    test("keeps run deliverables", () => {
        for (const rel of [
            "t/artifacts/sdd.md",
            "t/artifacts/recommendation.json",
            "t/artifacts/proj/main.flow",
            "t/artifacts/proj/project.uiproj",
            "t/artifacts/proj/workflow.xaml",
            "t/artifacts/.env.example",
        ]) {
            expect(isExcludedArtifact(rel), rel).toBe(false);
        }
    });

    test("mirrors fnmatch: * spans path separators", () => {
        // `*/node_modules/*` must match a deeply nested file.
        expect(isExcludedArtifact("a/b/c/node_modules/d/e.js")).toBe(true);
    });
});

describe("sortArtifacts", () => {
    const ref = (relPath: string, kind: string): ArtifactRef => ({
        relPath,
        kind,
        sizeBytes: 0,
    });

    test("deliverables first, then shallower paths, then alpha", () => {
        const input = [
            ref("d/artifacts/fixtures/deep/a.json", "json"),
            ref("d/artifacts/notes.txt", "txt"),
            ref("d/artifacts/proj/main.flow", "flow"),
            ref("d/artifacts/sdd.md", "md"),
        ];
        const order = sortArtifacts(input).map((a) => a.relPath);
        // .flow (deliverable kind) and sdd.md (deliverable name) rank first;
        // among them the shallower sdd.md wins. Non-deliverables follow, the
        // shallower notes.txt ahead of the deep fixtures file.
        expect(order).toEqual([
            "d/artifacts/sdd.md",
            "d/artifacts/proj/main.flow",
            "d/artifacts/notes.txt",
            "d/artifacts/fixtures/deep/a.json",
        ]);
    });

    test("does not mutate the input array", () => {
        const input = [ref("b.txt", "txt"), ref("a.flow", "flow")];
        sortArtifacts(input);
        expect(input.map((a) => a.relPath)).toEqual(["b.txt", "a.flow"]);
    });
});

describe("walkArtifacts", () => {
    let root: string;
    let target: string;

    beforeAll(async () => {
        // Layout mirrors a codex task workspace: a real deliverable, a nested
        // dir, plus a symlink to an external skill dir (the `.agents/skills/*`
        // scaffolding) and a symlink to a file. Both symlinks must be skipped.
        root = await fs.mkdtemp(path.join(os.tmpdir(), "walk-art-"));
        target = await fs.mkdtemp(path.join(os.tmpdir(), "walk-tgt-"));
        await fs.writeFile(path.join(target, "SKILL.md"), "# skill\n");

        await fs.writeFile(path.join(root, "main.flow"), "{}\n");
        await fs.mkdir(path.join(root, "proj"));
        await fs.writeFile(path.join(root, "proj", "app.cs"), "//\n");
        await fs.mkdir(path.join(root, ".agents", "skills"), {
            recursive: true,
        });
        await fs.symlink(target, path.join(root, ".agents", "skills", "uipath-rpa"));
        await fs.symlink(
            path.join(root, "main.flow"),
            path.join(root, "alias.flow"),
        );
    });

    afterAll(async () => {
        await fs.rm(root, { recursive: true, force: true });
        await fs.rm(target, { recursive: true, force: true });
    });

    test("skips symlinks (dir and file) and does not descend into them", async () => {
        const rels = (await walkArtifacts(root)).map((a) => a.relPath).sort();
        expect(rels).toEqual(["main.flow", "proj/app.cs"]);
        // No symlink entry, and the symlinked skill dir's SKILL.md never appears.
        expect(rels.some((r) => r.includes("uipath-rpa"))).toBe(false);
        expect(rels.some((r) => r.includes("SKILL.md"))).toBe(false);
        expect(rels).not.toContain("alias.flow");
    });
});

describe("clearRunCacheDir", () => {
    let root: string;

    beforeEach(async () => {
        root = await fs.mkdtemp(path.join(os.tmpdir(), "clear-cache-"));
    });
    afterEach(async () => {
        await fs.rm(root, { recursive: true, force: true });
    });

    test("removes the run's cache dir for a valid id", async () => {
        const runDir = path.join(root, "20260601T120000");
        await fs.mkdir(runDir, { recursive: true });
        await fs.writeFile(path.join(runDir, "run.json"), "{}\n");

        expect(await clearRunCacheDir(root, "20260601T120000")).toBe(true);
        await expect(fs.access(runDir)).rejects.toThrow();
    });

    test("never-cached run is a harmless no-op (still true)", async () => {
        expect(await clearRunCacheDir(root, "absent-run")).toBe(true);
    });

    test('rejects "." and ".." so rm cannot escape root', async () => {
        const marker = path.join(root, "keep.txt");
        await fs.writeFile(marker, "x");

        expect(await clearRunCacheDir(root, ".")).toBe(false);
        expect(await clearRunCacheDir(root, "..")).toBe(false);
        // Root (and thus its contents) untouched.
        await expect(fs.access(marker)).resolves.toBeUndefined();
        await expect(fs.access(root)).resolves.toBeUndefined();
    });

    test("rejects ids with path separators", async () => {
        expect(await clearRunCacheDir(root, "a/b")).toBe(false);
        expect(await clearRunCacheDir(root, "../sibling")).toBe(false);
    });
});

describe("visibleTurnsFromRaw (historical backfill)", () => {
    test("prefers the persisted visible_turns field", () => {
        expect(
            visibleTurnsFromRaw({
                visible_turns: 7,
                actual_commands: 99, // ignored when the field is present
                has_final_reply: true,
            }),
        ).toBe(7);
    });

    test("reconstructs from actual_commands + final reply on legacy runs", () => {
        expect(
            visibleTurnsFromRaw({ actual_commands: 4, has_final_reply: true }),
        ).toBe(5);
    });

    test("omits the +1 when there is no final reply", () => {
        expect(
            visibleTurnsFromRaw({ actual_commands: 4, has_final_reply: false }),
        ).toBe(4);
    });

    test("treats actual_commands=0 as a real count, not missing", () => {
        expect(
            visibleTurnsFromRaw({ actual_commands: 0, has_final_reply: true }),
        ).toBe(1);
    });

    test("null when neither the field nor actual_commands is present", () => {
        expect(visibleTurnsFromRaw({})).toBeNull();
        expect(visibleTurnsFromRaw({ has_final_reply: true })).toBeNull();
    });
});
