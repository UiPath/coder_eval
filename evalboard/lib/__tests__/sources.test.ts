import { describe, expect, test } from "vitest";
import {
    DEFAULT_SOURCE,
    SCRIBE_SOURCE,
    SKILLS_SOURCE,
    SOURCES,
    runsDirFor,
    sourceById,
} from "../sources";

describe("source registry", () => {
    test("skills is the default source and keeps the bare 'runs' container", () => {
        expect(DEFAULT_SOURCE).toBe(SKILLS_SOURCE);
        expect(SKILLS_SOURCE.container).toBe("runs");
    });

    test("scribe reads its own container, not the skills nightly's", () => {
        expect(SCRIBE_SOURCE.container).toBe("aria-runs");
        expect(SCRIBE_SOURCE.container).not.toBe(SKILLS_SOURCE.container);
    });

    test("every source has a distinct id and container", () => {
        const ids = SOURCES.map((s) => s.id);
        const containers = SOURCES.map((s) => s.container);
        expect(new Set(ids).size).toBe(SOURCES.length);
        expect(new Set(containers).size).toBe(SOURCES.length);
    });

    // Source ids land in filesystem paths (runsDirFor) and query strings, so they
    // must stay within the narrow charset blob.ts's ID_RE enforces for ids.
    test("ids are filesystem- and URL-safe", () => {
        for (const s of SOURCES) {
            expect(s.id).toMatch(/^[\w-]+$/);
        }
    });
});

describe("sourceById", () => {
    test("resolves a known id", () => {
        expect(sourceById("scribe")).toBe(SCRIBE_SOURCE);
        expect(sourceById("skills")).toBe(SKILLS_SOURCE);
    });

    // Coercing rather than throwing is deliberate: a stray ?src= in a shared URL
    // should show the default dashboard, not an error page. The tradeoff is that a
    // TYPO'd source silently shows skills data — asserted here so that behaviour
    // is a decision on record rather than an accident.
    test.each([undefined, null, "", "nope", "SCRIBE"])(
        "coerces %p to the default source",
        (input) => {
            expect(sourceById(input)).toBe(DEFAULT_SOURCE);
        },
    );
});

describe("runsDirFor", () => {
    test("default source uses the base dir unchanged", () => {
        expect(runsDirFor("/cache/runs-remote", SKILLS_SOURCE)).toBe(
            "/cache/runs-remote",
        );
    });

    // A nested cache dir would appear as an entry when listing the default
    // source's runs; a sibling cannot be mistaken for a run at all.
    test("non-default source gets a sibling dir, not a subdirectory", () => {
        const dir = runsDirFor("/cache/runs-remote", SCRIBE_SOURCE);
        expect(dir).toBe("/cache/runs-remote-scribe");
        expect(dir.startsWith("/cache/runs-remote/")).toBe(false);
    });

    // The whole reason sources cannot share a cache root: both suites name runs
    // `YYYY-MM-DD_HH-MM-SS`, so identical ids across containers are expected.
    test("distinct sources never share a cache dir", () => {
        const dirs = SOURCES.map((s) => runsDirFor("/cache/runs-remote", s));
        expect(new Set(dirs).size).toBe(SOURCES.length);
    });

    test("a trailing separator on the base does not produce a stray segment", () => {
        expect(runsDirFor("/cache/runs-remote/", SCRIBE_SOURCE)).toBe(
            "/cache/runs-remote-scribe",
        );
    });

    test("suffixes only the final segment, leaving parent dirs alone", () => {
        expect(runsDirFor("/cache/nested/runs-remote", SCRIBE_SOURCE)).toBe(
            "/cache/nested/runs-remote-scribe",
        );
    });

    test("a relative base stays relative", () => {
        expect(runsDirFor("runs-remote", SCRIBE_SOURCE)).toBe(
            "runs-remote-scribe",
        );
    });
});

// Regression guard for a real build break: lib/sources.ts is imported by CLIENT
// components (app/_lib/source-param.ts builds hrefs in task-grid, activation-card
// and refresh-button), so a Node builtin import here fails `next build` with
// UnhandledSchemeError — which tsc and vitest both happily pass. Catch it here so
// the failure surfaces in a fast test rather than only in the production build.
describe("client-safety", () => {
    test("sources.ts imports no Node builtins", async () => {
        const { readFile } = await import("node:fs/promises");
        const { join } = await import("node:path");
        // Resolved from the vitest root (evalboard/) rather than import.meta.url,
        // which is not a file: URL under vitest's transform.
        const src = await readFile(join(process.cwd(), "lib/sources.ts"), "utf-8");
        expect(src).not.toMatch(/from\s+["']node:/);
        expect(src).not.toMatch(/require\(\s*["']node:/);
    });
});
