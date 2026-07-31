// Traffic-light thresholds for a pass rate.
//
// These mirror the nightly Slack rollup's dots
// (coder_eval_uipath/eval_runner/scripts/ci/slack_summary.py, GREEN_PCT /
// RED_PCT) so a number that reads green in the channel reads green here. The
// channel is where a regression is first noticed and the evalboard is where it
// gets opened, so the two disagreeing about which numbers are healthy is worse
// than either bar being slightly off. Change these only alongside that script.
export const PASS_GREEN_PCT = 95;
export const PASS_RED_PCT = 85;

// "none" is the absence of a measurement, which callers signal by passing null.
// That is not the same as a measured 0% — that one is genuinely bad and must
// read red.
export type PassTone = "none" | "good" | "warn" | "bad";

export function passTone(pct: number | null | undefined): PassTone {
    if (pct == null || !Number.isFinite(pct)) return "none";
    if (pct >= PASS_GREEN_PCT) return "good";
    if (pct < PASS_RED_PCT) return "bad";
    return "warn";
}

const TEXT_CLASS: Record<PassTone, string> = {
    none: "text-gray-500",
    good: "text-green-700",
    warn: "text-amber-700",
    bad: "text-red-700",
};

// Fills (progress bars, meters) need more saturation than text to read at all,
// so they take the 500 step rather than 700.
const BAR_CLASS: Record<PassTone, string> = {
    none: "bg-gray-300",
    good: "bg-green-500",
    warn: "bg-amber-500",
    bad: "bg-red-500",
};

// Pass rate as a percent (0-100) -> Tailwind text color. Pass null when there is
// nothing to measure so it reads neutral rather than red.
export function passClass(pct: number | null | undefined): string {
    return TEXT_CLASS[passTone(pct)];
}

export function passBarClass(pct: number | null | undefined): string {
    return BAR_CLASS[passTone(pct)];
}

// Same, for the tables that carry a rate as a 0-1 fraction (trends, watchlist)
// rather than a percent.
export function passClassRatio(rate: number | null | undefined): string {
    return passClass(rate == null ? null : rate * 100);
}
