import type { MessageEvent } from "./runs";

// Wall-clock efficiency: how a task's duration compares to the time it is
// expected to take.
//
// `expected_seconds` is derived per task, per harness by the eval runner (p10 of
// past successful durations) and stamped into run.json. The dashboard reads that
// stamp rather than deriving its own, so a number here always matches the line the
// run was actually scored against.
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

// Ratio as a table cell: `1.8×`, or an em dash for an unscored task. One decimal,
// not two: the baseline is a min over a handful of runs (p10 over ten), and a
// task's own night-to-night spread is wider than the digit a second decimal adds.
export function fmtTimeRatioCell(ratio: number | null): string {
    return ratio == null ? "—" : `${ratio.toFixed(1)}×`;
}

// Title text for a duration cell: what the task was measured against, or why it
// was not measured at all.
export function expectedTimeTitle(expectedSeconds: number | null): string {
    return expectedSeconds != null
        ? `expected time: ${fmtTaskSeconds(expectedSeconds)}`
        : "no expected time yet (needs a passing run on this harness)";
}

// Epoch milliseconds for a `CommandTelemetry` timestamp, or null when it is
// absent or unparseable. The stamps are naive local ISO strings (Python
// `datetime.now()`), which `Date` reads as local time — every stamp in a run
// comes from one machine, and only DIFFERENCES are ever taken, so the offset
// cancels.
export function epochMs(value: string | null | undefined): number | null {
    if (typeof value !== "string" || !value) return null;
    const t = Date.parse(value);
    return Number.isFinite(t) ? t : null;
}

// Milliseconds inside [lo, hi] where at least ONE span was running: the UNION,
// not the sum.
//
// The TypeScript twin of `coder_eval.timing.busy_ms`, deliberately the
// same algorithm — the harness subtracts tool time from its generation windows
// with it (once, in coder_eval/timing.py::subtract_tool_time), and this file
// subtracts tool time from a task's wall clock, so the two must agree about
// what "tool execution took N ms" means. Held in step by
// tests/_fixtures/timing_union_cases.json, which both suites replay.
export function busyMs(
    spans: [number, number][],
    lo: number,
    hi: number,
): number {
    const clipped = spans
        .map(([s, e]) => [Math.max(s, lo), Math.min(e, hi)] as [number, number])
        .filter(([s, e]) => e > s)
        .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (clipped.length === 0) return 0;
    let total = 0;
    let [openStart, openEnd] = clipped[0];
    for (const [start, end] of clipped.slice(1)) {
        if (start > openEnd) {
            total += openEnd - openStart;
            [openStart, openEnd] = [start, end];
        } else {
            openEnd = Math.max(openEnd, end);
        }
    }
    return total + (openEnd - openStart);
}

// Wall-clock milliseconds these messages' tool calls occupied: the UNION of
// their BOUNDED execution intervals.
//
// Concurrent tools occupy the wall clock once, and summing them drove the task
// page's Unaccounted cell to -615ms on a task with two concurrent sleeps, where
// the honest answer was +2.5s of sandbox setup and grading.
//
// A call the harness TIMED but did not BOUND contributes nothing — no
// `durationMs` fallback. That is a policy, and it is the same one
// `coder_eval.timing.main_thread_tool_spans` has always had on the Python side:
// its `is not None` filter drops a command with no `execution_started_at` /
// `execution_completed_at`, so every Python surface already ignored these while
// this function folded them in. A duration with no bounds cannot be placed on
// the timeline, so it cannot be unioned with anything; adding it to a union
// double-books whatever it overlapped and can drive the residual negative,
// which destroys the disjointness the four-bucket identity rests on. Such time
// lands in Unaccounted instead, which is precisely what that cell is for — the
// harness measured a duration it cannot place.
//
// Measured blast radius: 9336 of 12170 commands in the run history on disk are
// unbounded, every one a codex `Bash`, ~8 h in aggregate. That population is
// CLOSED — codex went from 0% bounded before 2026-09-10 to 100% after — so no
// future run changes, but on historical codex runs this moves up to ~8 h out of
// Tool exec and into Unaccounted. Going forward the only harness reporting a
// bare duration is the out-of-tree `delegate-sdk`; see
// docs/agents/HARNESS_PARITY.md.
export function toolExecutionMs(messages: MessageEvent[]): number {
    const spans: [number, number][] = [];
    for (const m of messages) {
        for (const t of m.toolUses) {
            if (t.execStartMs != null && t.execEndMs != null) {
                spans.push([t.execStartMs, t.execEndMs]);
            }
        }
    }
    if (spans.length === 0) return 0;
    const lo = Math.min(...spans.map(([s]) => s));
    const hi = Math.max(...spans.map(([, e]) => e));
    return busyMs(spans, lo, hi);
}
