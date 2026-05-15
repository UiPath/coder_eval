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
import type { DailyPoint } from "@/lib/overview";

// Recharts uses --MM-DD for compact tick labels; full date appears in the tooltip.
function shortLabel(dateKey: string): string {
    const [, m, d] = dateKey.split("-");
    return `${m}-${d}`;
}

function CustomTooltip({
    active,
    payload,
    label,
}: {
    active?: boolean;
    payload?: Array<{ payload: DailyPoint }>;
    label?: string;
}) {
    if (!active || !payload?.length) return null;
    const point = payload[0].payload;
    return (
        <div className="bg-white border border-gray-200 rounded-md shadow-sm px-3 py-2 text-xs">
            <div className="font-medium text-gray-900 tabular-nums">
                {label}
            </div>
            <div className="text-gray-600 tabular-nums">
                {point.avgSuccessRate != null
                    ? `${point.avgSuccessRate.toFixed(1)}% success`
                    : "no runs"}
            </div>
            <div className="text-gray-500 tabular-nums">
                {point.runCount} run{point.runCount === 1 ? "" : "s"}
            </div>
        </div>
    );
}

export function DailySuccessChart({ data }: { data: DailyPoint[] }) {
    const display = data.map((p) => ({
        ...p,
        shortDate: shortLabel(p.date),
    }));
    return (
        <div className="w-full h-56 relative">
            <span className="absolute top-1 right-2 text-[10px] text-gray-400 tabular-nums">
                UTC
            </span>
            <ResponsiveContainer width="100%" height="100%">
                <LineChart
                    data={display}
                    margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
                >
                    <CartesianGrid stroke="#f3f4f6" vertical={false} />
                    <XAxis
                        dataKey="shortDate"
                        tick={{ fontSize: 11, fill: "#6b7280" }}
                        tickLine={false}
                        axisLine={{ stroke: "#e5e7eb" }}
                        interval="preserveStartEnd"
                        minTickGap={24}
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
                        type="monotone"
                        dataKey="avgSuccessRate"
                        stroke="#0d6efd"
                        strokeWidth={2}
                        dot={{ r: 3, fill: "#0d6efd" }}
                        activeDot={{ r: 4 }}
                        connectNulls={true}
                        isAnimationActive={false}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}
