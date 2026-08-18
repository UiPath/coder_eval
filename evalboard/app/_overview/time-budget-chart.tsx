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
// series split, plotting the per-run "within expected time" rate instead of the
// success rate. Driven by the same windowed RunPoint[] so the shared window and
// harness selectors control both.
export function WithinExpectedTimeChart({
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
        "withinExpectedTimeRate",
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
                            domain={[0, 100]}
                            ticks={[0, 25, 50, 75, 100]}
                            tick={{ fontSize: 11, fill: "#6b7280" }}
                            tickLine={false}
                            axisLine={false}
                            width={36}
                        />
                        <Tooltip
                            content={
                                <HarnessTooltip
                                    series={series}
                                    suffix="within expected time"
                                    emptyText="no scored tasks"
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
                                // Unlike the success chart this must bridge, for
                                // the same reason: rows are interleaved across
                                // harnesses, so "no value here" usually means
                                // "another harness's run". A run of this harness
                                // with no scored task is already absent from
                                // the pivot, so it reads as a bridged gap rather
                                // than a fabricated 0%.
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
