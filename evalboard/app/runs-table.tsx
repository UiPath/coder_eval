"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { RunSummary } from "@/lib/runs";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";

type SortKey = "task" | "duration" | "cost" | "tools" | "status";

function fmtDuration(s: number | null): string {
    if (s == null) return "—";
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    const rem = s - m * 60;
    return `${m}m${rem.toFixed(0).padStart(2, "0")}s`;
}

function fmtCost(c: number | null): string {
    if (c == null) return "—";
    return `$${c.toFixed(3)}`;
}

function statusRank(s: string | null): number {
    if (s === "FAILURE" || s === "ERROR") return 0;
    if (s === "SUCCESS") return 2;
    return 1;
}

const DEFAULT_DIR: Record<SortKey, "asc" | "desc"> = {
    task: "asc",
    duration: "desc",
    cost: "desc",
    tools: "desc",
    status: "asc",
};

function compare(a: RunSummary, b: RunSummary, key: SortKey): number {
    switch (key) {
        case "task":
            return (a.taskId ?? "").localeCompare(b.taskId ?? "");
        case "duration":
            return (
                (a.durationSeconds ?? -Infinity) -
                (b.durationSeconds ?? -Infinity)
            );
        case "cost":
            return (
                (a.totalCostUsd ?? -Infinity) - (b.totalCostUsd ?? -Infinity)
            );
        case "tools":
            return (
                (a.actualCommands ?? -Infinity) -
                (b.actualCommands ?? -Infinity)
            );
        case "status":
            return statusRank(a.status) - statusRank(b.status);
    }
}

const COLUMNS: Array<{
    key: SortKey;
    header: string;
    align?: "right";
}> = [
    { key: "task", header: "Task" },
    { key: "duration", header: "Duration", align: "right" },
    { key: "cost", header: "Cost", align: "right" },
    { key: "tools", header: "Tools", align: "right" },
    { key: "status", header: "Status" },
];

export function RunsTable({ runs }: { runs: RunSummary[] }) {
    const [sort, setSort] = useState<{
        key: SortKey;
        dir: "asc" | "desc";
    } | null>(null);

    const sorted = useMemo(() => {
        const arr = [...runs];
        if (sort) {
            arr.sort((a, b) => {
                const c = compare(a, b, sort.key);
                if (c !== 0) return sort.dir === "asc" ? c : -c;
                return b.id.localeCompare(a.id);
            });
        } else {
            arr.sort(
                (a, b) =>
                    statusRank(a.status) - statusRank(b.status) ||
                    b.id.localeCompare(a.id),
            );
        }
        return arr;
    }, [runs, sort]);

    const onSort = (key: SortKey) => {
        setSort((cur) =>
            cur?.key === key
                ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
                : { key, dir: DEFAULT_DIR[key] },
        );
    };

    return (
        <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
            <table className="w-full text-sm">
                <thead>
                    <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                        {COLUMNS.map((col) => {
                            const alignCls =
                                col.align === "right"
                                    ? "text-right"
                                    : "text-left";
                            const active = sort?.key === col.key;
                            const arrow = active
                                ? sort.dir === "asc"
                                    ? "▲"
                                    : "▼"
                                : "";
                            return (
                                <th
                                    key={col.key}
                                    className={`py-3 px-4 font-medium ${alignCls}`}
                                >
                                    <button
                                        type="button"
                                        onClick={() => onSort(col.key)}
                                        className="inline-flex items-center gap-1 hover:text-gray-900"
                                    >
                                        {col.header}
                                        <span className="text-xs text-gray-400 w-3">
                                            {arrow}
                                        </span>
                                    </button>
                                </th>
                            );
                        })}
                    </tr>
                </thead>
                <tbody>
                    {sorted.map((r) => {
                        const tags = r.tags.filter(
                            (t) => t !== "uipath-maestro-flow",
                        );
                        return (
                            <tr
                                key={r.id}
                                className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                            >
                                <td className="py-3 px-4 text-gray-700">
                                    <div className="flex items-center gap-3">
                                        <span className="inline-flex items-center justify-center w-8 h-8 rounded bg-gray-100 text-gray-500 text-xs font-semibold border border-gray-200 shrink-0">
                                            T
                                        </span>
                                        <div className="flex flex-col min-w-0">
                                            <div className="flex items-center gap-2 flex-wrap">
                                                <Link
                                                    href={`/runs/${r.id}`}
                                                    className="text-gray-900 hover:text-studio-blue font-semibold"
                                                >
                                                    {humanizeTaskId(r.taskId)}
                                                </Link>
                                                {tags.map((t) => (
                                                    <span
                                                        key={t}
                                                        className="text-[9px] uppercase tracking-wide text-gray-500 bg-gray-100 px-1 py-[1px] rounded"
                                                    >
                                                        {t}
                                                    </span>
                                                ))}
                                            </div>
                                            <span className="text-xs text-gray-500 tabular-nums">
                                                {fmtRunTime(r.id)}
                                            </span>
                                        </div>
                                    </div>
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                    {fmtDuration(r.durationSeconds)}
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                    {fmtCost(r.totalCostUsd)}
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                    {r.actualCommands ?? "—"}
                                </td>
                                <td className="py-3 px-4 text-gray-700">
                                    <StatusPill status={r.status} relabel />
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}
