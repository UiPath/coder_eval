import { describe, expect, test } from "vitest";
import {
    ALL_HARNESSES,
    DEFAULT_HARNESS,
    KNOWN_HARNESSES,
    harnessColor,
    orderHarnesses,
    parseHarnessParam,
    parseHarnessScope,
} from "../harness";

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

// parseHarnessScope is the overview's variant: the same untrusted `?h=` param,
// but null-defaulting so the front page opens on every harness instead of
// silently scoping to claude-code. Anything that mis-maps here mis-scopes the
// summary tiles, both charts, and the run table at once, since all four now read
// this one value.
describe("parseHarnessScope", () => {
    test("absent / empty input means all harnesses", () => {
        expect(parseHarnessScope(undefined)).toBeNull();
        expect(parseHarnessScope([])).toBeNull();
        expect(parseHarnessScope("")).toBeNull();
        expect(parseHarnessScope("   ")).toBeNull();
    });

    test("the explicit all sentinel means all harnesses", () => {
        expect(parseHarnessScope(ALL_HARNESSES)).toBeNull();
        expect(parseHarnessScope([ALL_HARNESSES])).toBeNull();
    });

    test("passes through any syntactically-valid id", () => {
        for (const id of [...KNOWN_HARNESSES, "gpt-5.5", "some_harness"]) {
            expect(parseHarnessScope(id)).toBe(id);
        }
    });

    test("trims before validating, and picks the first array element", () => {
        expect(parseHarnessScope("  codex  ")).toBe("codex");
        expect(parseHarnessScope(["  antigravity ", "codex"])).toBe(
            "antigravity",
        );
    });

    test("malformed ids widen to all rather than to a wrong harness", () => {
        // Contrast with parseHarnessParam, which falls back to claude-code. A
        // bad param here must not silently produce claude-code-only numbers.
        for (const bad of ["has space", "a/b", "bad;rm", "a".repeat(65)]) {
            expect(parseHarnessScope(bad)).toBeNull();
        }
    });

    test("differs from parseHarnessParam exactly on the empty case", () => {
        expect(parseHarnessParam(undefined)).toBe(DEFAULT_HARNESS);
        expect(parseHarnessScope(undefined)).toBeNull();
    });
});

// Color is bound to the harness, never to its index in the series list — a
// filter that removes one line must not repaint the survivors.
describe("harnessColor", () => {
    test("every known harness has its own reserved color", () => {
        const colors = KNOWN_HARNESSES.map(harnessColor);
        expect(new Set(colors).size).toBe(KNOWN_HARNESSES.length);
    });

    test("is stable per harness regardless of what else is on screen", () => {
        // The regression this guards: assigning by position, so dropping
        // claude-code would slide codex onto claude-code's blue.
        expect(harnessColor("codex")).toBe(harnessColor("codex"));
        expect(harnessColor("codex")).not.toBe(harnessColor("claude-code"));
    });

    test("unknown harnesses share one neutral, not a generated hue", () => {
        const a = harnessColor("brand-new-harness");
        const b = harnessColor("another-newcomer");
        expect(a).toBe(b);
        for (const known of KNOWN_HARNESSES) {
            expect(a).not.toBe(harnessColor(known));
        }
    });
});

describe("orderHarnesses", () => {
    test("known harnesses come first, in KNOWN_HARNESSES order", () => {
        expect(
            orderHarnesses(["antigravity", "claude-code", "codex"]),
        ).toEqual(["claude-code", "codex", "antigravity"]);
    });

    test("newcomers follow, alphabetically", () => {
        expect(
            orderHarnesses(["zeta", "codex", "alpha-harness"]),
        ).toEqual(["codex", "alpha-harness", "zeta"]);
    });

    test("dedupes and drops nothing else", () => {
        expect(orderHarnesses(["codex", "codex"])).toEqual(["codex"]);
        expect(orderHarnesses([])).toEqual([]);
    });
});
