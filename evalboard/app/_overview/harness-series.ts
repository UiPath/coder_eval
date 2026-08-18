// Pivot the flat RunPoint[] into the wide, one-column-per-harness shape recharts
// needs to draw several lines on one chart.
//
// Each run belongs to exactly one harness, so a row carries a value for that
// harness's column and leaves the others undefined. Every <Line> then uses
// connectNulls to bridge its own gaps, which is what turns sparse interleaved
// runs (claude-code nightly, codex twice a week) into one continuous line each
// rather than a single zigzag across incomparable harnesses.

import { harnessColor } from "@/lib/harness";
import type { RunPoint } from "@/lib/overview";

// Which per-run metric to plot. All of them live on the same RunPoint, so the
// overview charts share this module and differ only by this key.
export type HarnessMetric =
    | "successRate"
    | "withinExpectedTimeRate"
    | "timePerPassedTask";

export interface HarnessSeries {
    harness: string;
    // Column name in the pivoted rows. See seriesKey.
    dataKey: string;
    color: string;
}

export interface HarnessChartData {
    rows: Array<Record<string, number>>;
    series: HarnessSeries[];
}

// Sanitized because recharts reads a dataKey containing "." as a nested object
// path, which would resolve to undefined and silently draw no line. Prefixed
// with the series index because sanitizing alone is not injective — `a.b` and
// `a-b` would collide onto one column and one harness's data would vanish
// under the other's.
export function seriesKey(harness: string, index: number): string {
    return `h${index}_${harness.replace(/[^\w]/g, "_")}`;
}

export function pivotByHarness(
    points: RunPoint[],
    harnesses: string[],
    metric: HarnessMetric,
): HarnessChartData {
    const series: HarnessSeries[] = harnesses.map((harness, i) => ({
        harness,
        dataKey: seriesKey(harness, i),
        color: harnessColor(harness),
    }));
    const keyByHarness = new Map(series.map((s) => [s.harness, s.dataKey]));

    // Merge on timestamp so two harnesses that happen to start a run in the same
    // second share one row instead of producing two points recharts would render
    // stacked on the same x.
    const byTimestamp = new Map<number, Record<string, number>>();
    for (const p of points) {
        if (p[metric] == null) continue;
        // A point whose harness isn't in the requested series list has no column
        // to land in; dropping it keeps a stale id out of the chart.
        const key = keyByHarness.get(p.harness);
        if (!key) continue;
        let row = byTimestamp.get(p.timestamp);
        if (!row) {
            row = { timestamp: p.timestamp };
            byTimestamp.set(p.timestamp, row);
        }
        row[key] = p[metric] as number;
    }

    const rows = [...byTimestamp.values()].sort(
        (a, b) => a.timestamp - b.timestamp,
    );
    // Drop series with no plotted point in this window — an empty line would put
    // an entry in the legend for a harness the chart never draws.
    const present = series.filter((s) => rows.some((r) => s.dataKey in r));
    return { rows, series: present };
}
