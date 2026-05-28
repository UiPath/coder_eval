import { describe, expect, test } from "vitest";
import {
    type ArtifactRef,
    isExcludedArtifact,
    sortArtifacts,
    toTaskRow,
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
