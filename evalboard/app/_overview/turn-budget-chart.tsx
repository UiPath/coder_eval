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

// MM-DD tick label on the axis; full date+time appears in the tooltip.
function shortLabel(ms: number): string {
    const d = new Date(ms);
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    return `${m}-${day}`;
}

function fullLabel(ms: number): string {
    const d = new Date(ms);
    const y = d.getUTCFullYear();
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    const h = String(d.getUTCHours()).padStart(2, "0");
    const min = String(d.getUTCMinutes()).padStart(2, "0");
    return `${y}-${m}-${day} ${h}:${min} UTC`;
}

function CustomTooltip({
    active,
    payload,
}: {
    active?: boolean;
    payload?: Array<{ payload: RunPoint }>;
}) {
    if (!active || !payload?.length) return null;
    const point = payload[0].payload;
    return (
        <div className="bg-white border border-gray-200 rounded-md shadow-sm px-3 py-2 text-xs">
            <div className="font-medium text-gray-900 tabular-nums">
                {fullLabel(point.timestamp)}
            </div>
            <div className="text-gray-600 tabular-nums">
                {point.turnBudgetRate != null
                    ? `${point.turnBudgetRate.toFixed(1)}% within turn budget (budgeted failures count as over)`
                    : "no tasks with a turn budget"}
            </div>
        </div>
    );
}

// Sibling of DailySuccessChart: same axes/styling, plotting the per-run
// "within expected turns" rate instead of the success rate. Driven by the
// same windowed RunPoint[] so the shared window selector controls both.
export function TurnBudgetChart({
    data,
    windowStart,
    windowEnd,
}: {
    data: RunPoint[];
    windowStart: number;
    windowEnd: number;
}) {
    return (
        <div className="w-full h-56 relative">
            <span className="absolute top-1 right-2 text-[10px] text-gray-400 tabular-nums">
                UTC
            </span>
            <ResponsiveContainer width="100%" height="100%">
                <LineChart
                    data={data}
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
                        content={<CustomTooltip />}
                        cursor={{ stroke: "#e5e7eb", strokeDasharray: "3 3" }}
                    />
                    <Line
                        type="linear"
                        dataKey="turnBudgetRate"
                        stroke="#0d6efd"
                        strokeWidth={2}
                        dot={{ r: 3, fill: "#0d6efd" }}
                        activeDot={{ r: 4 }}
                        // Don't bridge runs that have no budgeted tasks — a gap
                        // is honest about missing data; a line is not.
                        connectNulls={false}
                        isAnimationActive={false}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}
