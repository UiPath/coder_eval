// Wall-clock efficiency: how a task's duration compares to the time it is
// expected to take.
//
// `expected_seconds` is derived per task, per harness by the eval runner (p10 of
// past successful durations) and stamped into run.json. The dashboard reads that
// stamp rather than deriving its own, so a number here still matches the ping
// that announced the run.
//
// A task with no `expected_seconds` is *unscored*, not "within budget": too young
// for history, or a run that predates stamping. Every helper returns null there.

export interface TimeRatioThresholds {
    yellow: number;
    red: number;
}

export function getTimeRatioThresholds(): TimeRatioThresholds {
    const parse = (raw: string | undefined, fallback: number) => {
        if (!raw) return fallback;
        const n = Number(raw);
        return Number.isFinite(n) && n > 0 ? n : fallback;
    };
    return {
        yellow: parse(process.env.EVALBOARD_TIME_YELLOW_RATIO, 1.5),
        red: parse(process.env.EVALBOARD_TIME_RED_RATIO, 2),
    };
}

export type TimeTint = "green" | "yellow" | "red" | null;

// Pure time-efficiency ratio (seconds ÷ expected_seconds), used to tint per-task
// duration cells. Blind to pass/fail on purpose: the cell answers "did this take
// longer than it should?", which holds either way. The aggregates diverge
// deliberately and score passes only (withinExpectedTime, overview.ts).
export function timeRatio(
    durationSeconds: number | null,
    expectedSeconds: number | null,
): number | null {
    if (
        durationSeconds == null ||
        expectedSeconds == null ||
        expectedSeconds <= 0
    ) {
        return null;
    }
    return durationSeconds / expectedSeconds;
}

export function tintForTimeRatio(
    ratio: number | null,
    t: TimeRatioThresholds = getTimeRatioThresholds(),
): TimeTint {
    if (ratio == null) return null;
    if (ratio > t.red) return "red";
    if (ratio > t.yellow) return "yellow";
    return "green";
}

export function timeCellClasses(tint: TimeTint): string {
    switch (tint) {
        case "green":
            return "text-emerald-700";
        case "yellow":
            return "text-amber-700";
        case "red":
            return "text-rose-700";
        default:
            return "text-gray-900";
    }
}

// A task counts as within its expected time while it stays inside
// (1 + tolerance) × expected, so anything past 2× its line reads slow. Mirrors
// `timing.TOLERANCE` on the runner side, which records the value it used in each
// run's `timing` block. Red tints at the same 2×, so the cell and the rollup agree.
export const TIME_BUDGET_TOLERANCE = 1;

// Whether a task came in at or under (1 + tolerance) × its expected time.
// Null when the task is not scoreable: no duration, or no positive
// `expected_seconds`.
export function withinExpectedTime(
    durationSeconds: number | null,
    expectedSeconds: number | null,
    tolerance: number = TIME_BUDGET_TOLERANCE,
): boolean | null {
    const ratio = timeRatio(durationSeconds, expectedSeconds);
    if (ratio == null) return null;
    return ratio <= 1 + tolerance;
}

// Per-task wall clock, to the second: `3m14s`, `1h02m` past the hour. Seconds
// are the point of the metric, so they are never rounded away below an hour.
export function fmtTaskSeconds(seconds: number | null): string {
    if (seconds == null) return "—";
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h
        ? `${h}h${String(m).padStart(2, "0")}m`
        : `${m}m${String(sec).padStart(2, "0")}s`;
}

export function fmtTimeRatio(ratio: number | null): string {
    return ratio == null ? "—" : `${ratio.toFixed(2)}x expected`;
}

// Title text for a duration cell: what the task was measured against, or why it
// was not measured at all.
export function expectedTimeTitle(expectedSeconds: number | null): string {
    return expectedSeconds != null
        ? `expected time: ${fmtTaskSeconds(expectedSeconds)}`
        : "no expected time yet (needs a passing run on this harness)";
}
