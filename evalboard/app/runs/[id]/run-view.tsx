"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { TaskResultSummary } from "@/lib/runs";
import { fmtDuration, humanizeTaskId } from "@/lib/format";
import { statusCategory } from "@/lib/status";
import { TaskGrid } from "./task-grid";

const TOP_N_TAGS = 10;
const Q_DEBOUNCE_MS = 300;

function parseTagsParam(raw: string | null): string[] {
    if (!raw) return [];
    return raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}

function Metric({
    label,
    value,
    sub,
    valueClass = "text-gray-900",
}: {
    label: string;
    value: string;
    sub?: string;
    valueClass?: string;
}) {
    return (
        <div className="bg-white border border-gray-200 rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                {label}
            </div>
            <div
                className={`text-2xl font-semibold mt-1 tabular-nums ${valueClass}`}
            >
                {value}
            </div>
            {sub && (
                <div className="text-xs text-gray-500 tabular-nums mt-0.5">
                    {sub}
                </div>
            )}
        </div>
    );
}

export function RunView({
    runId,
    tasks,
}: {
    runId: string;
    tasks: TaskResultSummary[];
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();

    const selectedTags = useMemo(
        () => parseTagsParam(searchParams.get("tags")),
        [searchParams],
    );
    const selectedSet = useMemo(() => new Set(selectedTags), [selectedTags]);

    const [q, setQ] = useState(() => searchParams.get("q") ?? "");
    const [showAllTags, setShowAllTags] = useState(false);

    // Sync q from URL when it changes externally (browser back/forward, manual
    // edit, etc.). Doesn't loop with the debounced write below: this effect
    // re-runs only when the URL string changes, and once URL and state agree
    // the write effect's early-return prevents a re-write.
    const urlQ = searchParams.get("q") ?? "";
    useEffect(() => {
        setQ((prev) => (prev.trim() === urlQ ? prev : urlQ));
    }, [urlQ]);

    // Debounced URL sync for q. Only fires when q diverges from the URL.
    useEffect(() => {
        const trimmed = q.trim();
        const current = searchParams.get("q") ?? "";
        if (trimmed === current) return;
        const timer = setTimeout(() => {
            const params = new URLSearchParams(searchParams.toString());
            if (trimmed) params.set("q", trimmed);
            else params.delete("q");
            const qs = params.toString();
            router.replace(qs ? `${pathname}?${qs}` : pathname, {
                scroll: false,
            });
        }, Q_DEBOUNCE_MS);
        return () => clearTimeout(timer);
    }, [q, pathname, router, searchParams]);

    const updateTags = useCallback(
        (next: string[]) => {
            const params = new URLSearchParams(searchParams.toString());
            if (next.length === 0) params.delete("tags");
            else params.set("tags", next.join(","));
            const qs = params.toString();
            router.replace(qs ? `${pathname}?${qs}` : pathname, {
                scroll: false,
            });
        },
        [pathname, router, searchParams],
    );

    const toggleTag = useCallback(
        (tag: string) => {
            updateTags(
                selectedSet.has(tag)
                    ? selectedTags.filter((t) => t !== tag)
                    : [...selectedTags, tag],
            );
        },
        [selectedSet, selectedTags, updateTags],
    );

    const clearAll = useCallback(() => {
        setQ("");
        updateTags([]);
    }, [updateTags]);

    const allTags = useMemo(() => {
        const counts = new Map<string, number>();
        for (const t of tasks) {
            for (const tag of t.tags) {
                counts.set(tag, (counts.get(tag) ?? 0) + 1);
            }
        }
        // Sort by count desc, then alphabetical for stability.
        return [...counts.entries()].sort(
            ([a, ac], [b, bc]) => bc - ac || a.localeCompare(b),
        );
    }, [tasks]);

    const qLower = q.trim().toLowerCase();

    const filtered = useMemo(() => {
        let arr = tasks;
        if (selectedTags.length > 0) {
            arr = arr.filter((t) =>
                selectedTags.every((tag) => t.tags.includes(tag)),
            );
        }
        if (qLower) {
            arr = arr.filter((t) => {
                if (t.taskId.toLowerCase().includes(qLower)) return true;
                if (
                    humanizeTaskId(t.taskId).toLowerCase().includes(qLower)
                )
                    return true;
                if (t.tags.some((tag) => tag.toLowerCase().includes(qLower)))
                    return true;
                return false;
            });
        }
        return arr;
    }, [tasks, selectedTags, qLower]);

    const isFiltered = selectedTags.length > 0 || qLower.length > 0;

    // Filter-aware metrics computed from `filtered`. Status categorization
    // is centralized in lib/status.ts and mirrors coder_eval FinalStatus.category.
    const metrics = useMemo(() => {
        const total = filtered.length;
        let passed = 0;
        let failed = 0;
        let errored = 0;
        let cost = 0;
        let durationSum = 0;
        let costKnown = false;
        let durKnown = false;
        for (const t of filtered) {
            const cat = statusCategory(t.status);
            if (cat === "passed") passed++;
            else if (cat === "error") errored++;
            else failed++;
            if (t.totalCostUsd != null) {
                cost += t.totalCostUsd;
                costKnown = true;
            }
            if (t.durationSeconds != null) {
                durationSum += t.durationSeconds;
                durKnown = true;
            }
        }
        return {
            total,
            passed,
            failed,
            errored,
            failedTotal: failed + errored,
            pct: total ? (passed / total) * 100 : 0,
            cost: costKnown ? cost : null,
            duration: durKnown ? durationSum : null,
        };
    }, [filtered]);

    // Top-N visible tags + sticky-selected tags not in top-N.
    const visibleTags = useMemo(() => {
        if (showAllTags) return allTags;
        const top = allTags.slice(0, TOP_N_TAGS);
        const topNames = new Set(top.map(([t]) => t));
        const sticky = allTags.filter(
            ([t]) => selectedSet.has(t) && !topNames.has(t),
        );
        return [...top, ...sticky];
    }, [allTags, showAllTags, selectedSet]);
    const hiddenCount = allTags.length - visibleTags.length;

    const emptyHint = (() => {
        if (selectedTags.length > 0 && !qLower) {
            return `no tasks match all selected tags (${selectedTags.join(" + ")})`;
        }
        if (qLower && selectedTags.length === 0) {
            return `no tasks match "${q.trim()}"`;
        }
        if (isFiltered) return "no tasks match the current filter";
        return "no tasks in this run";
    })();

    return (
        <div className="space-y-5">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div className="col-span-2 bg-white border border-gray-200 rounded-lg p-4">
                    <div className="text-xs text-gray-500 uppercase tracking-wide">
                        Pass rate
                        {isFiltered && (
                            <span className="ml-2 text-studio-blue normal-case tracking-normal">
                                · filtered
                            </span>
                        )}
                    </div>
                    <div className="flex items-baseline gap-2 mt-1">
                        <span className="text-2xl font-semibold text-gray-900 tabular-nums">
                            {metrics.pct.toFixed(0)}%
                        </span>
                        <span className="text-sm text-gray-500 tabular-nums">
                            {metrics.passed} / {metrics.total}
                        </span>
                    </div>
                    <div className="mt-3 h-2 bg-gray-100 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-green-500"
                            style={{ width: `${metrics.pct}%` }}
                        />
                    </div>
                </div>
                <Metric
                    label="Failed"
                    value={String(metrics.failedTotal)}
                    sub={
                        metrics.errored
                            ? `${metrics.failed} fail · ${metrics.errored} error`
                            : undefined
                    }
                    valueClass={
                        metrics.failedTotal > 0
                            ? "text-red-700"
                            : "text-gray-900"
                    }
                />
                <Metric
                    label="Total cost"
                    value={
                        metrics.cost != null
                            ? `$${metrics.cost.toFixed(2)}`
                            : "—"
                    }
                />
                <Metric
                    label="Time"
                    value={fmtDuration(metrics.duration)}
                />
            </div>

            <div className="flex flex-col md:flex-row md:flex-wrap md:items-start gap-x-3 gap-y-2">
                <div className="relative w-full md:w-72 md:shrink-0">
                    <input
                        type="text"
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                        placeholder="Search task or tag…"
                        aria-label="Search tasks or tags"
                        className="w-full text-sm border border-gray-200 rounded-md px-3 py-1.5 pr-7 focus:outline-none focus:border-studio-blue focus:ring-1 focus:ring-studio-blue"
                    />
                    {q && (
                        <button
                            type="button"
                            onClick={() => setQ("")}
                            aria-label="Clear search"
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
                        >
                            ×
                        </button>
                    )}
                </div>

                {allTags.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 md:flex-1 md:min-w-0">
                        <span className="text-xs text-gray-500 mr-1">
                            Tags
                            {selectedTags.length > 1 ? " (all match)" : ""}:
                        </span>
                        {visibleTags.map(([tag, count]) => {
                            const active = selectedSet.has(tag);
                            const cls = active
                                ? "bg-studio-blue text-white border-studio-blue"
                                : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50";
                            return (
                                <button
                                    key={tag}
                                    type="button"
                                    onClick={() => toggleTag(tag)}
                                    aria-pressed={active}
                                    className={`text-xs px-2 py-0.5 rounded border transition-colors ${cls}`}
                                >
                                    {tag}
                                    <span
                                        className={`ml-1 tabular-nums ${active ? "text-white/80" : "text-gray-400"}`}
                                    >
                                        {count}
                                    </span>
                                </button>
                            );
                        })}
                        {!showAllTags && hiddenCount > 0 && (
                            <button
                                type="button"
                                onClick={() => setShowAllTags(true)}
                                className="text-xs px-2 py-0.5 rounded border border-dashed border-gray-300 text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors"
                            >
                                +{hiddenCount} more
                            </button>
                        )}
                        {showAllTags && allTags.length > TOP_N_TAGS && (
                            <button
                                type="button"
                                onClick={() => setShowAllTags(false)}
                                className="text-xs px-2 py-0.5 rounded border border-dashed border-gray-300 text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors"
                            >
                                show less
                            </button>
                        )}
                    </div>
                )}
            </div>

            <div className="flex items-baseline gap-3 pt-1">
                <h2 className="text-sm font-semibold text-gray-900">Tasks</h2>
                <span className="text-xs text-gray-500 tabular-nums">
                    {isFiltered
                        ? `${filtered.length} / ${tasks.length}`
                        : `${tasks.length} total · click to open`}
                </span>
                {isFiltered && (
                    <button
                        type="button"
                        onClick={clearAll}
                        className="text-xs text-gray-500 hover:text-gray-900 underline"
                    >
                        clear filter
                    </button>
                )}
            </div>

            <TaskGrid
                runId={runId}
                tasks={filtered}
                selectedSet={selectedSet}
                onToggleTag={toggleTag}
                emptyHint={emptyHint}
            />
        </div>
    );
}
