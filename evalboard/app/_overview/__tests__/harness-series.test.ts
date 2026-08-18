import { describe, expect, test } from "vitest";
import { harnessColor } from "@/lib/harness";
import type { RunPoint } from "@/lib/overview";
import { pivotByHarness, seriesKey } from "../harness-series";

// pivotByHarness is the only thing standing between "one line per harness" and
// the zigzag the charts used to draw across incomparable harnesses. Its output
// is consumed by recharts, so a wrong key or a dropped row shows up as a missing
// line rather than an error — nothing else would fail.

function point(
    harness: string,
    timestamp: number,
    successRate: number | null,
    withinExpectedTimeRate: number | null = null,
): RunPoint {
    return {
        runId: `run-${timestamp}`,
        timestamp,
        harness,
        successRate,
        withinExpectedTimeRate,
        timePerPassedTask: null,
    };
}

describe("seriesKey", () => {
    test("prefixes and sanitizes so recharts can't read it as a path", () => {
        // A dataKey containing "." is a nested-object path in recharts, which
        // would resolve to undefined and silently draw no line.
        expect(seriesKey("gpt-5.5", 0)).not.toContain(".");
        expect(seriesKey("claude-code", 0)).toBe("h0_claude_code");
    });

    test("is injective even when two ids sanitize alike", () => {
        // Without the index prefix "a.b" and "a-b" both become "h_a_b", and one
        // harness's line would silently take over the other's column.
        const ids = ["claude-code", "codex", "gpt-5.5", "gpt-5-5"];
        const keys = ids.map((id, i) => seriesKey(id, i));
        expect(new Set(keys).size).toBe(ids.length);
    });
});

describe("pivotByHarness", () => {
    test("gives each harness its own column, keyed by run timestamp", () => {
        const { rows, series } = pivotByHarness(
            [point("claude-code", 100, 90), point("codex", 200, 70)],
            ["claude-code", "codex"],
            "successRate",
        );
        expect(series.map((s) => s.harness)).toEqual(["claude-code", "codex"]);
        const [cc, cx] = series.map((s) => s.dataKey);
        expect(rows).toEqual([
            { timestamp: 100, [cc]: 90 },
            { timestamp: 200, [cx]: 70 },
        ]);
        // The absent key on each row is what connectNulls bridges — that is how
        // interleaved runs become one continuous line per harness.
        expect(rows[0]).not.toHaveProperty(cx);
    });

    test("colors come from the harness, not the series index", () => {
        const { series } = pivotByHarness(
            [point("codex", 100, 70)],
            ["codex"],
            "successRate",
        );
        // codex is slot 2, but it is the ONLY series here. If color were assigned
        // positionally it would come back as slot 1's blue.
        expect(series[0].color).toBe(harnessColor("codex"));
        expect(series[0].color).not.toBe(harnessColor("claude-code"));
    });

    test("merges two harnesses that share a timestamp into one row", () => {
        const { rows, series } = pivotByHarness(
            [point("claude-code", 100, 90), point("codex", 100, 70)],
            ["claude-code", "codex"],
            "successRate",
        );
        const [cc, cx] = series.map((s) => s.dataKey);
        expect(rows).toEqual([{ timestamp: 100, [cc]: 90, [cx]: 70 }]);
    });

    test("sorts rows by timestamp regardless of input order", () => {
        const { rows } = pivotByHarness(
            [point("codex", 300, 1), point("codex", 100, 2)],
            ["codex"],
            "successRate",
        );
        expect(rows.map((r) => r.timestamp)).toEqual([100, 300]);
    });

    test("reads the requested metric, not the other one", () => {
        // Both charts share this pivot and the same RunPoint[], differing only
        // by the metric key — reading the wrong one would put the success rate
        // on the turn-budget chart with no visible error.
        const pts = [point("codex", 100, 90, 40)];
        const key = seriesKey("codex", 0);
        expect(pivotByHarness(pts, ["codex"], "successRate").rows[0]).toEqual({
            timestamp: 100,
            [key]: 90,
        });
        expect(pivotByHarness(pts, ["codex"], "withinExpectedTimeRate").rows[0]).toEqual(
            { timestamp: 100, [key]: 40 },
        );
    });

    test("omits a null metric rather than plotting it as zero", () => {
        // The turn-budget metric is null for a run with no budgeted task. A 0%
        // there would read as "every task blew its budget", which is the
        // opposite of "there was nothing to measure".
        const { rows } = pivotByHarness(
            [point("codex", 100, 90, null)],
            ["codex"],
            "withinExpectedTimeRate",
        );
        expect(rows).toEqual([]);
    });

    test("drops series with no plotted point so the legend stays honest", () => {
        // antigravity is a known harness with no run in this window; an empty
        // line would still put it in the legend.
        const { series } = pivotByHarness(
            [point("codex", 100, 70)],
            ["codex", "antigravity"],
            "successRate",
        );
        expect(series.map((s) => s.harness)).toEqual(["codex"]);
    });

    test("keeps a harness whose only value is a real zero", () => {
        // 0% is a legitimate pass rate (every task failed) and must not be
        // confused with "no data" by a truthiness check.
        const { rows, series } = pivotByHarness(
            [point("codex", 100, 0)],
            ["codex"],
            "successRate",
        );
        expect(series).toHaveLength(1);
        expect(rows[0][series[0].dataKey]).toBe(0);
    });

    test("handles an empty window", () => {
        expect(pivotByHarness([], ["codex"], "successRate")).toEqual({
            rows: [],
            series: [],
        });
    });
});
