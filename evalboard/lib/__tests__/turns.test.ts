import { afterEach, describe, expect, test, vi } from "vitest";
import {
    TURN_BUDGET_TOLERANCE,
    fmtTurnsCount,
    getTurnRatioThresholds,
    tintForRatio,
    turnRatio,
    withinTurnBudget,
} from "../turns";

describe("turnRatio", () => {
    test("computes sdk / expected", () => {
        expect(turnRatio(8, 5)).toBe(1.6);
    });

    test("null when sdk missing", () => {
        expect(turnRatio(null, 5)).toBeNull();
    });

    test("null when expected missing", () => {
        expect(turnRatio(8, null)).toBeNull();
    });

    test("null when expected is zero (defensive)", () => {
        expect(turnRatio(8, 0)).toBeNull();
    });
});

describe("tintForRatio (defaults: yellow=1.25, red=1.5)", () => {
    test("green at 0.5", () => {
        expect(tintForRatio(0.5, { yellow: 1.25, red: 1.5 })).toBe("green");
    });

    test("green at 1.2 (under +25%)", () => {
        expect(tintForRatio(1.2, { yellow: 1.25, red: 1.5 })).toBe("green");
    });

    test("yellow at 1.4 (between +25% and +50%)", () => {
        expect(tintForRatio(1.4, { yellow: 1.25, red: 1.5 })).toBe("yellow");
    });

    test("red at 2.0", () => {
        expect(tintForRatio(2.0, { yellow: 1.25, red: 1.5 })).toBe("red");
    });

    test("green at exactly 1.25 (yellow boundary)", () => {
        expect(tintForRatio(1.25, { yellow: 1.25, red: 1.5 })).toBe("green");
    });

    test("yellow at exactly 1.5 (red boundary)", () => {
        expect(tintForRatio(1.5, { yellow: 1.25, red: 1.5 })).toBe("yellow");
    });

    test("null ratio → null tint", () => {
        expect(tintForRatio(null)).toBeNull();
    });
});

describe("getTurnRatioThresholds", () => {
    afterEach(() => {
        vi.unstubAllEnvs();
    });

    test("returns defaults when env unset", () => {
        vi.stubEnv("EVALBOARD_TURNS_YELLOW_RATIO", "");
        vi.stubEnv("EVALBOARD_TURNS_RED_RATIO", "");
        expect(getTurnRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });

    test("honours env overrides", () => {
        vi.stubEnv("EVALBOARD_TURNS_YELLOW_RATIO", "1.1");
        vi.stubEnv("EVALBOARD_TURNS_RED_RATIO", "1.75");
        expect(getTurnRatioThresholds()).toEqual({ yellow: 1.1, red: 1.75 });
    });

    test("falls back to defaults on non-numeric env", () => {
        vi.stubEnv("EVALBOARD_TURNS_YELLOW_RATIO", "not-a-number");
        vi.stubEnv("EVALBOARD_TURNS_RED_RATIO", "high");
        expect(getTurnRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });

    test("falls back to defaults on zero / negative env", () => {
        vi.stubEnv("EVALBOARD_TURNS_YELLOW_RATIO", "0");
        vi.stubEnv("EVALBOARD_TURNS_RED_RATIO", "-1");
        expect(getTurnRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });
});

describe("withinTurnBudget (default tolerance 0.5 → 1.5× budget)", () => {
    test("default tolerance is 0.5", () => {
        expect(TURN_BUDGET_TOLERANCE).toBe(0.5);
    });

    test("passes at exactly 1.5× the budget", () => {
        expect(withinTurnBudget(15, 10)).toBe(true);
    });

    test("fails just past 1.5× the budget", () => {
        expect(withinTurnBudget(16, 10)).toBe(false);
    });

    test("passes well under budget", () => {
        expect(withinTurnBudget(7, 10)).toBe(true);
    });

    test("null when no visible-turn count (ineligible)", () => {
        expect(withinTurnBudget(null, 10)).toBeNull();
    });

    test("null when no budget (ineligible)", () => {
        expect(withinTurnBudget(12, null)).toBeNull();
    });

    test("null when budget below 1 (defensive)", () => {
        expect(withinTurnBudget(0, 0)).toBeNull();
    });

    test("honours a custom tolerance", () => {
        expect(withinTurnBudget(10, 10, 0)).toBe(true);
        expect(withinTurnBudget(11, 10, 0)).toBe(false);
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
