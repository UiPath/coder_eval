import { describe, expect, test } from "vitest";
import { displayedTurns, fmtTurnsCount } from "../turns";

describe("displayedTurns", () => {
    test("counts tool calls plus the final reply", () => {
        expect(displayedTurns(4, true)).toBe(5);
    });

    test("counts tool calls alone when there is no final reply", () => {
        expect(displayedTurns(4, false)).toBe(4);
    });

    test("a reply with no recorded commands still counts as one turn", () => {
        expect(displayedTurns(null, true)).toBe(1);
    });

    test("null when there is nothing to count", () => {
        expect(displayedTurns(null, false)).toBeNull();
    });
});

describe("fmtTurnsCount", () => {
    test("renders the count when set", () => {
        expect(fmtTurnsCount(7)).toBe("7");
    });

    test("renders em dash when null", () => {
        expect(fmtTurnsCount(null)).toBe("—");
    });

    test("renders zero as 0 (not em dash)", () => {
        expect(fmtTurnsCount(0)).toBe("0");
    });
});
