import { describe, expect, test } from "vitest";
import { DEFAULT_HARNESS, parseHarnessParam } from "../harness";

// parseHarnessParam gates the untrusted `?h=` query param at every
// harness-scoped route, and its result decides which runs are counted toward
// the pass-rate/trend numbers — so an over-permissive charset or a wrong
// default would silently mis-scope the board with no other failing assertion.
describe("parseHarnessParam", () => {
    test("picks the first element of an array param", () => {
        expect(parseHarnessParam(["codex", "antigravity"])).toBe("codex");
    });

    test("falls back to the default for absent / empty input", () => {
        expect(parseHarnessParam(undefined)).toBe(DEFAULT_HARNESS);
        expect(parseHarnessParam([])).toBe(DEFAULT_HARNESS);
        expect(parseHarnessParam("")).toBe(DEFAULT_HARNESS);
    });

    test("trims surrounding whitespace before validating", () => {
        expect(parseHarnessParam("  codex  ")).toBe("codex");
        expect(parseHarnessParam(["  antigravity "])).toBe("antigravity");
    });

    test("passes through any syntactically-valid id (not whitelisted)", () => {
        // A brand-new harness must be selectable the moment its runs exist,
        // so the parser must NOT reject ids outside KNOWN_HARNESSES.
        for (const id of [
            "claude-code",
            "codex",
            "antigravity",
            "delegate",
            "gpt-5.5",
            "some_harness",
        ]) {
            expect(parseHarnessParam(id)).toBe(id);
        }
    });

    test("rejects malformed ids back to the default", () => {
        expect(parseHarnessParam("has space")).toBe(DEFAULT_HARNESS);
        expect(parseHarnessParam("a/b")).toBe(DEFAULT_HARNESS);
        expect(parseHarnessParam("bad;rm")).toBe(DEFAULT_HARNESS);
    });

    test("enforces the 1-64 char length bound", () => {
        expect(parseHarnessParam("a".repeat(64))).toBe("a".repeat(64));
        expect(parseHarnessParam("a".repeat(65))).toBe(DEFAULT_HARNESS);
    });
});
