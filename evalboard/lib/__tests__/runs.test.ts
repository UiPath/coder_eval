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
    aggregateSubAgentUsage,
    type ArtifactRef,
    clearRunCacheDir,
    extractComponentShas,
    tallyModels,
    extractRunConfig,
    findMatureSourceRuns,
    isExcludedArtifact,
    type MessageEvent,
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

describe("aggregateSubAgentUsage", () => {
    // Per-sub-agent usage is derived by grouping the parsed messages on
    // `parentToolUseId` (the spawning Agent call's tool_use_id). Main-thread
    // messages (null/undefined) are skipped; a sub-agent's multiple generations
    // sum into one bucket, keyed by that tool_use_id.
    const msg = (over: Partial<MessageEvent>): MessageEvent =>
        ({
            parentToolUseId: null,
            inputTokens: 0,
            outputTokens: 0,
            cacheWriteTokens: 0,
            cacheReadTokens: 0,
            ...over,
        }) as MessageEvent;

    test("groups by parentToolUseId and sums each sub-agent's generations", () => {
        const messages = [
            // Main-thread message — must be skipped.
            msg({ parentToolUseId: null, inputTokens: 999, outputTokens: 999 }),
            msg({
                parentToolUseId: "call_a",
                inputTokens: 47,
                outputTokens: 121,
                cacheWriteTokens: 234,
                cacheReadTokens: 14349,
            }),
            // call_b has two generations that must sum into one bucket.
            msg({ parentToolUseId: "call_b", inputTokens: 10, outputTokens: 20, cacheReadTokens: 100 }),
            msg({ parentToolUseId: "call_b", inputTokens: 0, outputTokens: 6, cacheWriteTokens: 5 }),
        ];

        const result = aggregateSubAgentUsage(messages);

        expect(Object.keys(result).sort()).toEqual(["call_a", "call_b"]);
        expect(result["call_a"]).toEqual({
            input: 47,
            output: 121,
            cacheCreation: 234,
            cacheRead: 14349,
            // total = 47 + 121 + 234 + 14349
            total: 14751,
        });
        expect(result["call_b"]).toEqual({
            input: 10,
            output: 26,
            cacheCreation: 5,
            cacheRead: 100,
            total: 141,
        });
    });

    test("no sub-agent messages yields an empty breakdown", () => {
        expect(aggregateSubAgentUsage([])).toEqual({});
        expect(aggregateSubAgentUsage([msg({ parentToolUseId: null })])).toEqual({});
        expect(aggregateSubAgentUsage([msg({ parentToolUseId: undefined })])).toEqual({});
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

describe("extractComponentShas", () => {
    const baseEnv = {
        git_commit: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        skills_git_commit: "b2c3d4e",
        cli_version: "1.2.0-alpha.20260603.7393",
    };

    test("appends one chip per tool plugin after the core components", () => {
        const out = extractComponentShas({
            ...baseEnv,
            tool_plugins: {
                "orchestrator-tool": "1.2.0-alpha.20260603.7393",
                "maestro-tool": "1.2.0-alpha.20260603.7393",
            },
        });
        expect(out.map((c) => c.name)).toEqual([
            "coder_eval",
            "skills",
            "cli",
            "maestro-tool",
            "orchestrator-tool",
        ]);
        const maestro = out.find((c) => c.name === "maestro-tool");
        expect(maestro?.sha).toBe("1.2.0-alpha.20260603.7393");
        expect(maestro?.url).toBe(
            "https://github.com/UiPath/cli/pkgs/npm/maestro-tool/versions",
        );
    });

    test("legacy runs without tool_plugins are unchanged", () => {
        const out = extractComponentShas(baseEnv);
        expect(out.map((c) => c.name)).toEqual(["coder_eval", "skills", "cli"]);
    });

    test("skips empty/non-string plugin versions", () => {
        const out = extractComponentShas({
            ...baseEnv,
            tool_plugins: { "maestro-tool": "" },
        });
        expect(out.map((c) => c.name)).toEqual(["coder_eval", "skills", "cli"]);
    });

    test("drops components whose value is 'unknown' (in-container git SHAs)", () => {
        // Per-task env_info captured in the sandbox can't `git rev-parse` the
        // coder_eval / skills checkouts, so those come back "unknown"; only the
        // npm-resolved cli + tool plugins survive.
        const out = extractComponentShas({
            git_commit: "unknown",
            skills_git_commit: "unknown",
            cli_version: "1.2.0-alpha.20260604.7394",
            tool_plugins: { "maestro-tool": "1.2.0-alpha.20260604.7394" },
        });
        expect(out.map((c) => c.name)).toEqual(["cli", "maestro-tool"]);
    });

    // The framework is pinned and consumed by released version (env_info
    // `coder_eval`), not by checkout — so the chip must be that version and link
    // to its release, rather than a git SHA pointing at a tree.
    test("coder_eval reports its released version, linked to the release", () => {
        const out = extractComponentShas({
            ...baseEnv,
            coder_eval: "0.9.1",
        });
        const framework = out.find((c) => c.name === "coder_eval");
        expect(framework?.sha).toBe("0.9.1");
        expect(framework?.url).toBe(
            "https://github.com/UiPath/coder_eval/releases/tag/v0.9.1",
        );
    });

    test("the version wins over a git_commit present in the same run", () => {
        // Both keys are captured on every run; the SHA must not shadow the
        // version just because it comes first in env_info.
        const out = extractComponentShas({
            git_commit: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            coder_eval: "0.10.0",
        });
        expect(out.find((c) => c.name === "coder_eval")?.sha).toBe("0.10.0");
    });

    test("falls back to the git SHA when no version was captured", () => {
        // Legacy runs, and editable checkouts between releases, have only the
        // SHA — better a tree link than no chip at all.
        const out = extractComponentShas(baseEnv);
        const framework = out.find((c) => c.name === "coder_eval");
        expect(framework?.sha).toBe(baseEnv.git_commit);
        expect(framework?.url).toBe(
            `https://github.com/UiPath/coder_eval/tree/${baseEnv.git_commit}`,
        );
    });

    test("an 'unknown' version still falls through to the SHA", () => {
        const out = extractComponentShas({
            ...baseEnv,
            coder_eval: "unknown",
        });
        expect(out.find((c) => c.name === "coder_eval")?.sha).toBe(
            baseEnv.git_commit,
        );
    });
});

// The run header claims a single harness and model for the whole run; tallyModels
// is what keeps that claim honest when an A/B experiment fans variants across
// models inside one run.
describe("tallyModels", () => {
    const row = (model: string | null) => ({
        task_id: "t",
        model_used: model,
    });

    test("returns the most common model and the distinct count", () => {
        const out = tallyModels([
            row("claude-sonnet-5"),
            row("claude-sonnet-5"),
            row("claude-opus-5"),
        ]);
        expect(out).toEqual({ dominant: "claude-sonnet-5", distinct: 2 });
    });

    test("a single-model run reports distinct 1", () => {
        expect(tallyModels([row("claude-sonnet-5"), row("claude-sonnet-5")]))
            .toEqual({ dominant: "claude-sonnet-5", distinct: 1 });
    });

    test("ignores rows with no model rather than counting them as one", () => {
        const out = tallyModels([row(null), row("codex-mini"), row(null)]);
        expect(out).toEqual({ dominant: "codex-mini", distinct: 1 });
    });

    test("an empty or model-less run reports nothing to display", () => {
        expect(tallyModels([])).toEqual({ dominant: null, distinct: 0 });
        expect(tallyModels([row(null)])).toEqual({
            dominant: null,
            distinct: 0,
        });
    });
});

describe("extractRunConfig", () => {
    test("prefers the run-level RunConfig stamp (harness + environment)", () => {
        const out = extractRunConfig({
            environment_info: {
                run_config: {
                    harness: "codex",
                    model: "gpt-5.4",
                    environment: "prod",
                },
            },
        });
        expect(out.harness).toBe("codex");
        expect(out.environment).toBe("prod");
    });

    test("falls back to the most common per-task agent_config.type", () => {
        const out = extractRunConfig({
            task_results: [
                { task_id: "a", agent_config: { type: "antigravity" } },
                { task_id: "b", agent_config: { type: "antigravity" } },
                { task_id: "c", agent_config: { type: "claude-code" } },
            ],
        });
        expect(out.harness).toBe("antigravity");
        // environment is only known from the run-level stamp, never derived.
        expect(out.environment).toBeNull();
    });

    test("null harness when nothing identifies it (legacy runs)", () => {
        const out = extractRunConfig({ task_results: [{ task_id: "a" }] });
        expect(out.harness).toBeNull();
        expect(out.environment).toBeNull();
    });
});

describe("findMatureSourceRuns", () => {
    // Newest-first run list with injected run.json readers (the deps test seam).
    const ids = ["r5", "r4", "r3", "r2", "r1"];
    const runs: Record<
        string,
        { task_results: { task_id: string; mature_skipped?: boolean }[] }
    > = {
        r5: { task_results: [{ task_id: "a", mature_skipped: true }] },
        r4: { task_results: [{ task_id: "a", mature_skipped: true }] },
        r3: { task_results: [{ task_id: "a" }] }, // a last really ran here
        r2: { task_results: [{ task_id: "a" }] },
        r1: { task_results: [{ task_id: "a" }] },
    };
    const deps = {
        listIds: async () => ids,
        readRun: async (id: string) => runs[id] ?? null,
    };

    test("resolves the most recent earlier run that actually executed the task", async () => {
        const out = await findMatureSourceRuns(["a"], "r5", deps);
        // Walks r4 (also skipped) → r3 (executed) and stops there.
        expect(out).toEqual({ a: "r3" });
    });

    test("returns {} immediately when there are no mature tasks", async () => {
        let listed = false;
        const out = await findMatureSourceRuns([], "r5", {
            listIds: async () => {
                listed = true;
                return ids;
            },
            readRun: deps.readRun,
        });
        expect(out).toEqual({});
        expect(listed).toBe(false); // no reads at all
    });

    test("absent when the source run id is not in the list", async () => {
        expect(await findMatureSourceRuns(["a"], "nope", deps)).toEqual({});
    });

    test("a task with no earlier execution is omitted (stays non-clickable)", async () => {
        const onlyB = {
            listIds: async () => ["r2", "r1"],
            readRun: async (id: string) =>
                ({
                    r2: { task_results: [{ task_id: "a", mature_skipped: true }] },
                    r1: { task_results: [{ task_id: "a", mature_skipped: true }] },
                })[id] ?? null,
        };
        expect(await findMatureSourceRuns(["a"], "r2", onlyB)).toEqual({});
    });
});
