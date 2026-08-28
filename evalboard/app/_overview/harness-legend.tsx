"use client";

// Legend + tooltip shared by the two overview charts. Both are multi-series
// whenever the page is unscoped, and a multi-series chart must never identify a
// line by color alone — so the legend pairs each swatch with the harness's
// vendor badge and short label, and the tooltip repeats that pairing per row.

import { harnessShortLabel, HarnessBadge } from "@/app/_components/harness-badge";
import type { HarnessSeries } from "./harness-series";

export function HarnessLegend({ series }: { series: HarnessSeries[] }) {
    // One series needs no legend box — the chart's own heading names it.
    if (series.length < 2) return null;
    return (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pt-1">
            {series.map((s) => (
                <span
                    key={s.harness}
                    className="inline-flex items-center gap-1.5 text-[11px] text-gray-600"
                >
                    <span
                        aria-hidden
                        className="inline-block h-0.5 w-4 rounded-full"
                        style={{ backgroundColor: s.color }}
                    />
                    <HarnessBadge harness={s.harness} size={14} />
                    {harnessShortLabel(s.harness)}
                </span>
            ))}
        </div>
    );
}

// Recharts hands every series to the tooltip, including the ones with no value
// at the hovered x. Only the harnesses that actually ran at this timestamp are
// worth a row.
export interface TooltipEntry {
    dataKey?: string | number;
    value?: number | string | null;
    color?: string;
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

export function HarnessTooltip({
    active,
    payload,
    label,
    series,
    suffix,
    emptyText,
    format = (v) => `${v.toFixed(1)}%`,
    secondary,
}: {
    active?: boolean;
    payload?: TooltipEntry[];
    // The x value under the cursor; recharts passes the XAxis dataKey's value.
    label?: number | string;
    series: HarnessSeries[];
    // Trailing prose after the number, e.g. "success".
    suffix: string;
    // Shown when the hovered run produced no value for this metric at all.
    emptyText: string;
    // How to render the value. Defaults to a percentage; a seconds chart passes
    // its own duration formatter.
    format?: (value: number) => string;
    // Optional second line for a row, e.g. a companion rate the chart doesn't
    // plot. Returning null omits it for that point.
    secondary?: (harness: string, timestamp: number) => string | null;
}) {
    if (!active || !payload?.length) return null;
    const byKey = new Map(series.map((s) => [s.dataKey, s]));
    const rows = payload.filter(
        (e) => typeof e.value === "number" && byKey.has(String(e.dataKey)),
    );
    const ms = typeof label === "number" ? label : Number(label);
    return (
        <div className="bg-white border border-gray-200 rounded-md shadow-sm px-3 py-2 text-xs">
            {Number.isFinite(ms) && (
                <div className="font-medium text-gray-900 tabular-nums">
                    {fullLabel(ms)}
                </div>
            )}
            {rows.length === 0 ? (
                <div className="text-gray-600">{emptyText}</div>
            ) : (
                rows.map((e) => {
                    const s = byKey.get(String(e.dataKey))!;
                    const sub = secondary?.(s.harness, ms) ?? null;
                    return (
                        <div key={s.harness} className="tabular-nums">
                            <div className="flex items-center gap-1.5 text-gray-600">
                                <span
                                    aria-hidden
                                    className="inline-block h-0.5 w-3 rounded-full shrink-0"
                                    style={{ backgroundColor: s.color }}
                                />
                                <span className="text-gray-700">
                                    {harnessShortLabel(s.harness)}
                                </span>
                                <span>
                                    {format(e.value as number)} {suffix}
                                </span>
                            </div>
                            {sub && (
                                <div className="pl-[1.125rem] text-gray-500">
                                    {sub}
                                </div>
                            )}
                        </div>
                    );
                })
            )}
        </div>
    );
}
