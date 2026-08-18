"use client";

import {
    CartesianGrid,
    Line,
    LineChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from "recharts";
import type { RunPoint } from "@/lib/overview";
import { fmtTaskSeconds } from "@/lib/timing";
import { pivotByHarness } from "./harness-series";
import { HarnessLegend, HarnessTooltip } from "./harness-legend";

// MM-DD tick label on the axis; full date+time appears in the tooltip.
function shortLabel(ms: number): string {
    const d = new Date(ms);
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    return `${m}-${day}`;
}

// Sibling of DailySuccessChart: same axes/styling and the same per-harness
// series split, plotting seconds per passed task instead of the success rate.
// Driven by the same windowed RunPoint[] so the shared window and harness
// selectors control both. Per-harness lines matter more here than on any other
// chart: codex runs the suite in roughly a third of claude-code's wall clock,
// so one blended line would plot the schedule rather than the speed.
export function TimePerPassedTaskChart({
    data,
    harnesses,
    windowStart,
    windowEnd,
}: {
    data: RunPoint[];
    harnesses: string[];
    windowStart: number;
    windowEnd: number;
}) {
    const { rows, series } = pivotByHarness(
        data,
        harnesses,
        "timePerPassedTask",
    );
    // The within-expected rate belongs to one run, so it reads as a hover detail
    // on that run's point rather than as a headline over a multi-run chart.
    const withinByPoint = new Map(
        data
            .filter((p) => p.withinExpectedTimeRate != null)
            .map((p) => [
                `${p.harness}|${p.timestamp}`,
                p.withinExpectedTimeRate as number,
            ]),
    );
    return (
        <div className="space-y-1">
            <div className="w-full h-56 relative">
                <span className="absolute top-1 right-2 text-[10px] text-gray-400 tabular-nums">
                    UTC
                </span>
                <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                        data={rows}
                        margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
                    >
                        <CartesianGrid stroke="#f3f4f6" vertical={false} />
                        <XAxis
                            type="number"
                            dataKey="timestamp"
                            domain={[windowStart, windowEnd]}
                            tickFormatter={shortLabel}
                            tick={{ fontSize: 11, fill: "#6b7280" }}
                            tickLine={false}
                            axisLine={{ stroke: "#e5e7eb" }}
                            minTickGap={32}
                            scale="time"
                        />
                        <YAxis
                            // Zero-based: these are seconds, so the distance from
                            // the axis is the magnitude a reader is entitled to
                            // compare across harnesses.
                            domain={[0, "auto"]}
                            tickFormatter={(v: number) => fmtTaskSeconds(v)}
                            tick={{ fontSize: 11, fill: "#6b7280" }}
                            tickLine={false}
                            axisLine={false}
                            width={48}
                        />
                        <Tooltip
                            content={
                                <HarnessTooltip
                                    series={series}
                                    suffix="per passed task"
                                    emptyText="no passing tasks"
                                    format={fmtTaskSeconds}
                                    secondary={(harness, ms) => {
                                        const r = withinByPoint.get(
                                            `${harness}|${ms}`,
                                        );
                                        return r == null
                                            ? null
                                            : `${r.toFixed(0)}% within 2× expected`;
                                    }}
                                />
                            }
                            cursor={{
                                stroke: "#e5e7eb",
                                strokeDasharray: "3 3",
                            }}
                        />
                        {series.map((s) => (
                            <Line
                                key={s.dataKey}
                                type="linear"
                                dataKey={s.dataKey}
                                stroke={s.color}
                                strokeWidth={2}
                                dot={{ r: 3, fill: s.color }}
                                activeDot={{ r: 4 }}
                                // Rows are interleaved across harnesses, so "no
                                // value here" usually means "another harness's
                                // run". A run of this harness where nothing
                                // passed is already absent from the pivot, so it
                                // reads as a bridged gap rather than a
                                // fabricated 0s.
                                connectNulls={true}
                                isAnimationActive={false}
                            />
                        ))}
                    </LineChart>
                </ResponsiveContainer>
            </div>
            <HarnessLegend series={series} />
        </div>
    );
}
