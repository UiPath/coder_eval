"use client";

import type { AxisDomain } from "recharts/types/util/types";
import {
    CartesianGrid,
    Line,
    LineChart,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from "recharts";
import { fmtUsd, fmtUtcDay, fmtUtcMinute } from "@/lib/format";
import { fmtTaskSeconds } from "@/lib/timing";
import type { TrendLine, TrendRow } from "@/lib/series";

export type ChartUnit = "percent" | "share" | "points" | "usd" | "seconds";

const FORMAT: Record<ChartUnit, (v: number) => string> = {
    percent: (v) => `${v.toFixed(1)}%`,
    share: (v) => `${v.toFixed(1)}%`,
    points: (v) => `${v > 0 ? "+" : ""}${v.toFixed(1)}`,
    usd: (v) => fmtUsd(v),
    seconds: (v) => fmtTaskSeconds(v),
};

const TICK: Record<ChartUnit, (v: number) => string> = {
    percent: (v) => `${v}%`,
    share: (v) => `${v}%`,
    points: (v) => `${v > 0 ? "+" : ""}${v}`,
    usd: (v) => fmtUsd(v),
    seconds: (v) => fmtTaskSeconds(v),
};

const DOMAIN: Record<Exclude<ChartUnit, "percent">, AxisDomain> = {
    share: [0, "auto"],
    points: [
        (min: number) => Math.min(0, Math.floor(min)),
        (max: number) => Math.max(0, Math.ceil(max)),
    ],
    usd: [0, "auto"],
    seconds: [0, "auto"],
};

const HALF_DAY_MS = 43_200_000;

function xDomain(rows: readonly TrendRow[]): [number, number] {
    const times = rows.map((r) => r.timestamp);
    const lo = Math.min(...times);
    const hi = Math.max(...times);
    return lo === hi ? [lo - HALF_DAY_MS, hi + HALF_DAY_MS] : [lo, hi];
}

function percentAxis(rows: readonly TrendRow[]): {
    domain: [number, number];
    ticks: number[];
} {
    const values = rows.flatMap((r) =>
        Object.entries(r).flatMap(([k, v]) => (k === "timestamp" ? [] : [v])),
    );
    const lo = Math.max(0, Math.floor((Math.min(...values) - 5) / 10) * 10);
    if (lo === 0) return { domain: [0, 100], ticks: [0, 25, 50, 75, 100] };
    const ticks: number[] = [];
    for (let t = lo; t <= 100; t += 10) ticks.push(t);
    return { domain: [lo, 100], ticks };
}

function ChartTooltip({
    active,
    payload,
    label,
    lines,
    unit,
}: {
    active?: boolean;
    payload?: { dataKey?: string | number; value?: number | string | null }[];
    label?: number | string;
    lines: readonly TrendLine[];
    unit: ChartUnit;
}) {
    if (!active || !payload?.length) return null;
    const byKey = new Map(lines.map((l) => [l.key, l]));
    const ms = Number(label);
    return (
        <div className="bg-white border border-gray-200 rounded-md shadow-sm px-3 py-2 text-xs tabular-nums">
            {Number.isFinite(ms) && (
                <div className="font-medium text-gray-900">
                    {fmtUtcMinute(ms)} UTC
                </div>
            )}
            {payload.map((e) => {
                const line = byKey.get(String(e.dataKey));
                if (!line || typeof e.value !== "number") return null;
                return (
                    <div
                        key={line.key}
                        className="flex items-center gap-1.5 text-gray-600"
                    >
                        <span
                            aria-hidden
                            className="inline-block h-0.5 w-3 rounded-full shrink-0"
                            style={{ backgroundColor: line.color }}
                        />
                        <span className="text-gray-700">{line.label}</span>
                        <span>{FORMAT[unit](e.value)}</span>
                    </div>
                );
            })}
        </div>
    );
}

export function TrendChart({
    rows,
    lines,
    unit,
}: {
    rows: readonly TrendRow[];
    lines: readonly TrendLine[];
    unit: ChartUnit;
}) {
    const y: { domain: AxisDomain; ticks?: number[] } =
        unit === "percent" ? percentAxis(rows) : { domain: DOMAIN[unit] };
    return (
        <div className="space-y-1">
            <div className="w-full h-56 relative">
                <span className="absolute top-1 right-2 text-[10px] text-gray-400 tabular-nums">
                    UTC
                </span>
                <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                        data={[...rows]}
                        margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
                    >
                        <CartesianGrid stroke="#f3f4f6" vertical={false} />
                        <XAxis
                            type="number"
                            dataKey="timestamp"
                            domain={xDomain(rows)}
                            padding={{ left: 12, right: 12 }}
                            tickFormatter={fmtUtcDay}
                            tick={{ fontSize: 11, fill: "#6b7280" }}
                            tickLine={false}
                            axisLine={{ stroke: "#e5e7eb" }}
                            minTickGap={32}
                            scale="time"
                        />
                        <YAxis
                            domain={y.domain}
                            ticks={y.ticks}
                            tickFormatter={TICK[unit]}
                            tick={{ fontSize: 11, fill: "#6b7280" }}
                            tickLine={false}
                            axisLine={false}
                            width={unit === "seconds" ? 48 : 40}
                        />
                        {unit === "points" && (
                            <ReferenceLine y={0} stroke="#9ca3af" />
                        )}
                        <Tooltip
                            content={<ChartTooltip lines={lines} unit={unit} />}
                            cursor={{ stroke: "#e5e7eb", strokeDasharray: "3 3" }}
                        />
                        {lines.map((l) => (
                            <Line
                                key={l.key}
                                type="linear"
                                dataKey={l.key}
                                stroke={l.color}
                                strokeWidth={2}
                                dot={{ r: 3, fill: l.color }}
                                activeDot={{ r: 4 }}
                                connectNulls
                                isAnimationActive={false}
                            />
                        ))}
                    </LineChart>
                </ResponsiveContainer>
            </div>
            {lines.length > 1 && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pt-1">
                    {lines.map((l) => (
                        <span
                            key={l.key}
                            className="inline-flex items-center gap-1.5 text-[11px] text-gray-600"
                        >
                            <span
                                aria-hidden
                                className="inline-block h-0.5 w-4 rounded-full"
                                style={{ backgroundColor: l.color }}
                            />
                            {l.label}
                        </span>
                    ))}
                </div>
            )}
        </div>
    );
}
