import { describe, expect, test } from "vitest";
import {
    PASS_GREEN_PCT,
    PASS_RED_PCT,
    passBarClass,
    passClass,
    passClassRatio,
    passTone,
} from "../pass-rate";

// These cutoffs are shared with the nightly Slack rollup's traffic-light dots.
// A run that pings the channel red and then reads green on the page it links to
// is the failure this module exists to prevent, so the boundaries are pinned
// here rather than left to whatever each call site felt like.
describe("passTone", () => {
    test("mirrors the Slack rollup's cutoffs", () => {
        expect(PASS_GREEN_PCT).toBe(95);
        expect(PASS_RED_PCT).toBe(85);
    });

    test("is inclusive at green and exclusive at red", () => {
        expect(passTone(95)).toBe("good");
        expect(passTone(94.9)).toBe("warn");
        expect(passTone(85)).toBe("warn");
        expect(passTone(84.9)).toBe("bad");
    });

    test("treats a measured 0% as bad, not as missing data", () => {
        // Every task failed. A falsy check here would paint the worst possible
        // run the same neutral gray as a run that hasn't started.
        expect(passTone(0)).toBe("bad");
        expect(passClass(0)).toBe("text-red-700");
    });

    test("distinguishes no data from a low rate", () => {
        expect(passTone(null)).toBe("none");
        expect(passTone(undefined)).toBe("none");
        expect(passTone(50, false)).toBe("none");
        expect(passClass(null)).toBe("text-gray-500");
    });

    test("does not paint a tone from a non-finite rate", () => {
        // 0/0 in a caller's own division arrives here as NaN.
        expect(passTone(Number.NaN)).toBe("none");
    });

    test("clears 100% and anything above it", () => {
        expect(passTone(100)).toBe("good");
    });
});

describe("class helpers", () => {
    test("bars are more saturated than text at the same tone", () => {
        // Text at the 500 step is too light to read; a 2px-tall bar at the 700
        // step is too dark to distinguish from its track.
        expect(passClass(99)).toBe("text-green-700");
        expect(passBarClass(99)).toBe("bg-green-500");
        expect(passBarClass(90)).toBe("bg-amber-500");
        expect(passBarClass(10)).toBe("bg-red-500");
    });

    test("every tone maps to a distinct class", () => {
        const classes = [passClass(99), passClass(90), passClass(10), passClass(null)];
        expect(new Set(classes).size).toBe(4);
    });

    test("the ratio variant scales 0-1 onto the same cutoffs", () => {
        // trends/watchlist carry rates as fractions; feeding one in unscaled
        // would put every task in the red band.
        expect(passClassRatio(0.96)).toBe(passClass(96));
        expect(passClassRatio(0.9)).toBe(passClass(90));
        expect(passClassRatio(0.5)).toBe(passClass(50));
        expect(passClassRatio(1)).toBe("text-green-700");
        expect(passClassRatio(null)).toBe("text-gray-500");
    });
});
