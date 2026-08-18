import { afterEach, describe, expect, test, vi } from "vitest";
import {
    TIME_BUDGET_TOLERANCE,
    expectedTimeTitle,
    fmtTaskSeconds,
    fmtTimeRatio,
    getTimeRatioThresholds,
    timeRatio,
    tintForTimeRatio,
    withinExpectedTime,
} from "../timing";

describe("timeRatio", () => {
    test("computes actual / expected", () => {
        expect(timeRatio(160, 100)).toBe(1.6);
    });

    test("null when the duration is missing", () => {
        expect(timeRatio(null, 100)).toBeNull();
    });

    test("null when the task is unscored", () => {
        expect(timeRatio(160, null)).toBeNull();
    });

    test("null when expected is zero (defensive)", () => {
        expect(timeRatio(160, 0)).toBeNull();
    });
});

describe("tintForTimeRatio (defaults: yellow=1.25, red=1.5)", () => {
    const t = { yellow: 1.25, red: 1.5 };

    test("green well under the line", () => {
        expect(tintForTimeRatio(0.5, t)).toBe("green");
    });

    test("green at exactly 1.25 (yellow boundary)", () => {
        expect(tintForTimeRatio(1.25, t)).toBe("green");
    });

    test("yellow between the thresholds", () => {
        expect(tintForTimeRatio(1.4, t)).toBe("yellow");
    });

    test("yellow at exactly 1.5 (red boundary)", () => {
        expect(tintForTimeRatio(1.5, t)).toBe("yellow");
    });

    test("red past the red threshold", () => {
        expect(tintForTimeRatio(2.0, t)).toBe("red");
    });

    test("an unscored task is untinted, not green", () => {
        expect(tintForTimeRatio(null)).toBeNull();
    });
});

describe("getTimeRatioThresholds", () => {
    afterEach(() => {
        vi.unstubAllEnvs();
    });

    test("returns defaults when env unset", () => {
        vi.stubEnv("EVALBOARD_TIME_YELLOW_RATIO", "");
        vi.stubEnv("EVALBOARD_TIME_RED_RATIO", "");
        expect(getTimeRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });

    test("honours env overrides", () => {
        vi.stubEnv("EVALBOARD_TIME_YELLOW_RATIO", "1.1");
        vi.stubEnv("EVALBOARD_TIME_RED_RATIO", "1.75");
        expect(getTimeRatioThresholds()).toEqual({ yellow: 1.1, red: 1.75 });
    });

    test("falls back to defaults on non-numeric env", () => {
        vi.stubEnv("EVALBOARD_TIME_YELLOW_RATIO", "not-a-number");
        vi.stubEnv("EVALBOARD_TIME_RED_RATIO", "high");
        expect(getTimeRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });

    test("falls back to defaults on zero / negative env", () => {
        vi.stubEnv("EVALBOARD_TIME_YELLOW_RATIO", "0");
        vi.stubEnv("EVALBOARD_TIME_RED_RATIO", "-1");
        expect(getTimeRatioThresholds()).toEqual({ yellow: 1.25, red: 1.5 });
    });
});

describe("withinExpectedTime (default tolerance 0.5 → 1.5× expected)", () => {
    test("default tolerance matches the runner's", () => {
        expect(TIME_BUDGET_TOLERANCE).toBe(0.5);
    });

    test("within at exactly 1.5× expected", () => {
        expect(withinExpectedTime(150, 100)).toBe(true);
    });

    test("over just past 1.5× expected", () => {
        expect(withinExpectedTime(151, 100)).toBe(false);
    });

    test("within well under expected", () => {
        expect(withinExpectedTime(70, 100)).toBe(true);
    });

    test("null when there is no duration", () => {
        expect(withinExpectedTime(null, 100)).toBeNull();
    });

    test("null for an unscored task — never a verdict", () => {
        expect(withinExpectedTime(120, null)).toBeNull();
    });

    test("honours a custom tolerance", () => {
        expect(withinExpectedTime(100, 100, 0)).toBe(true);
        expect(withinExpectedTime(101, 100, 0)).toBe(false);
    });
});

describe("fmtTaskSeconds", () => {
    test("keeps seconds under an hour", () => {
        expect(fmtTaskSeconds(194)).toBe("3m14s");
        expect(fmtTaskSeconds(720)).toBe("12m00s");
        expect(fmtTaskSeconds(57)).toBe("0m57s");
    });

    test("switches to hours and minutes past the hour", () => {
        expect(fmtTaskSeconds(3720)).toBe("1h02m");
    });

    test("renders em dash when null", () => {
        expect(fmtTaskSeconds(null)).toBe("—");
    });
});

describe("fmtTimeRatio / expectedTimeTitle", () => {
    test("ratio reads as a multiple of expected", () => {
        expect(fmtTimeRatio(2.632)).toBe("2.63x expected");
        expect(fmtTimeRatio(null)).toBe("—");
    });

    test("an unscored task explains why it has no line", () => {
        expect(expectedTimeTitle(100)).toBe("expected time: 1m40s");
        expect(expectedTimeTitle(null)).toContain("no expected time yet");
    });
});
