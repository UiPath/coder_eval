"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { TaskResultSummary } from "@/lib/runs";
import type { ReviewIndexEntry } from "@/lib/reviews-types";
import { humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";
import { statusSortRank } from "@/lib/status";
import { ReviewChips } from "./review-chips";

type SortKey = "task" | "status" | "score" | "duration" | "cost" | "tools";

function fmtTableDuration(s: number | null): string {
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

const DEFAULT_DIR: Record<SortKey, "asc" | "desc"> = {
    task: "asc",
    status: "asc",
    score: "desc",
    duration: "desc",
    cost: "desc",
    tools: "desc",
};

function compare(
    a: TaskResultSummary,
    b: TaskResultSummary,
    key: SortKey,
): number {
    switch (key) {
        case "task":
            return a.taskId.localeCompare(b.taskId);
        case "status":
            return statusSortRank(a.status) - statusSortRank(b.status);
        case "score":
            return (
                (a.weightedScore ?? -Infinity) -
                (b.weightedScore ?? -Infinity)
            );
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
    }
}

const COLUMNS: Array<{
    key: SortKey;
    header: string;
    align?: "right";
}> = [
    { key: "task", header: "Task" },
    { key: "status", header: "Status" },
    { key: "score", header: "Score", align: "right" },
    { key: "duration", header: "Duration", align: "right" },
    { key: "cost", header: "Cost", align: "right" },
    { key: "tools", header: "Tools", align: "right" },
];

export function TaskGrid({
    runId,
    tasks,
    selectedSet,
    onToggleTag,
    emptyHint = "no tasks in this run",
    reviewsByTask,
    reviewSelectedSet,
    onToggleReviewTag,
}: {
    runId: string;
    tasks: TaskResultSummary[];
    selectedSet?: Set<string>;
    onToggleTag?: (tag: string) => void;
    emptyHint?: string;
    reviewsByTask?: Map<string, ReviewIndexEntry>;
    reviewSelectedSet?: Set<string>;
    onToggleReviewTag?: (tag: string) => void;
}) {
    const [sort, setSort] = useState<{
        key: SortKey;
        dir: "asc" | "desc";
    } | null>(null);

    const sorted = useMemo(() => {
        const arr = [...tasks];
        if (sort) {
            arr.sort((a, b) => {
                const c = compare(a, b, sort.key);
                if (c !== 0) return sort.dir === "asc" ? c : -c;
                return a.taskId.localeCompare(b.taskId);
            });
        } else {
            // Default: failures first, then by task id.
            arr.sort(
                (a, b) =>
                    statusSortRank(a.status) - statusSortRank(b.status) ||
                    a.taskId.localeCompare(b.taskId),
            );
        }
        return arr;
    }, [tasks, sort]);

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
                            const ariaSort: "ascending" | "descending" | "none" =
                                active
                                    ? sort.dir === "asc"
                                        ? "ascending"
                                        : "descending"
                                    : "none";
                            return (
                                <th
                                    key={col.key}
                                    aria-sort={ariaSort}
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
                    {sorted.map((t) => {
                        const review = reviewsByTask?.get(t.taskId);
                        return (
                        <tr
                            key={t.taskId}
                            className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                        >
                            <td className="py-3 px-4 text-gray-700">
                                <div className="flex flex-col min-w-0 gap-0.5">
                                    <Link
                                        href={`/runs/${runId}/${t.taskId}`}
                                        className="text-gray-900 hover:text-studio-blue font-semibold"
                                    >
                                        {humanizeTaskId(t.taskId)}
                                    </Link>
                                    {t.tags.length > 0 && (
                                        <div className="flex flex-wrap gap-1 mt-0.5">
                                            {t.tags.map((tag) => {
                                                const active =
                                                    selectedSet?.has(tag) ??
                                                    false;
                                                const cls = active
                                                    ? "bg-studio-blue/10 text-studio-blue border-studio-blue/30"
                                                    : "bg-gray-50 text-gray-500 border-gray-200 hover:bg-gray-100";
                                                const baseCls = `text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${cls}`;
                                                return onToggleTag ? (
                                                    <button
                                                        key={tag}
                                                        type="button"
                                                        onClick={() =>
                                                            onToggleTag(tag)
                                                        }
                                                        aria-pressed={active}
                                                        className={baseCls}
                                                    >
                                                        {tag}
                                                    </button>
                                                ) : (
                                                    <span
                                                        key={tag}
                                                        className={baseCls}
                                                    >
                                                        {tag}
                                                    </span>
                                                );
                                            })}
                                        </div>
                                    )}
                                    {review && review.tags.length > 0 && (
                                        <ReviewChips
                                            tags={review.tags}
                                            title={review.summary_excerpt}
                                            selectedSet={reviewSelectedSet}
                                            onToggleTag={onToggleReviewTag}
                                        />
                                    )}
                                </div>
                            </td>
                            <td className="py-3 px-4">
                                <StatusPill status={t.status} relabel />
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {t.weightedScore != null
                                    ? t.weightedScore.toFixed(2)
                                    : "—"}
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {fmtTableDuration(t.durationSeconds)}
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {fmtCost(t.totalCostUsd)}
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {t.actualCommands ?? "—"}
                            </td>
                        </tr>
                        );
                    })}
                    {sorted.length === 0 && (
                        <tr>
                            <td
                                colSpan={COLUMNS.length}
                                className="py-6 px-4 text-center text-sm text-gray-500"
                            >
                                {emptyHint}
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
