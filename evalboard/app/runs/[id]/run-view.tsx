"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import type { ActivationScore, TaskResultSummary } from "@/lib/runs";
import type { ReviewIndexEntry } from "@/lib/reviews-types";
import { fmtDuration, humanizeTaskId } from "@/lib/format";
import { statusCategory } from "@/lib/status";
import { ChipLegend } from "@/app/_overview/tag-rail";
import { CollapsibleRail } from "@/app/_components/collapsible-rail";
import { ActivationCard } from "./activation-card";
import { ChipButton } from "./chips";
import { TaskGrid } from "./task-grid";

const TOP_N_TAGS = 10;
const TOP_N_SKILLS = 20;

function percentile(values: number[], p: number): number | null {
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => a - b);
    // Linear interpolation between closest ranks.
    const idx = (sorted.length - 1) * p;
    const lo = Math.floor(idx);
    const hi = Math.ceil(idx);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

export interface RunMetrics {
    total: number;
    passed: number;
    failed: number;
    errored: number;
    failedTotal: number;
    pct: number;
    cost: number | null;
    costP50: number | null;
    costP90: number | null;
    duration: number | null;
    durationP50: number | null;
    durationP90: number | null;
}

// Aggregate run-level metrics from a set of task rows. Status categorization is
// centralized in lib/status.ts and mirrors coder_eval FinalStatus.category.
//
// Mature-skipped tasks are carried forward as passes (so they count toward
// total / passed / pct) but never ran this run. Their 0 cost / 0 duration would
// deflate the totals and drag p50/p90 toward zero, so they are excluded from
// both the cost/duration totals and the percentile samples — every cost and
// duration figure reflects only the tasks that actually executed. Mirrors the
// same exclusion in lib/trends.ts::aggregate.
export function computeRunMetrics(tasks: TaskResultSummary[]): RunMetrics {
    const total = tasks.length;
    let passed = 0;
    let failed = 0;
    let errored = 0;
    let cost = 0;
    let durationSum = 0;
    const costSamples: number[] = [];
    const durSamples: number[] = [];
    for (const t of tasks) {
        const cat = statusCategory(t.status);
        if (cat === "passed") passed++;
        else if (cat === "error") errored++;
        else failed++;
        if (t.matureSkipped) continue;
        if (t.totalCostUsd != null) {
            cost += t.totalCostUsd;
            costSamples.push(t.totalCostUsd);
        }
        if (t.durationSeconds != null) {
            durationSum += t.durationSeconds;
            durSamples.push(t.durationSeconds);
        }
    }
    return {
        total,
        passed,
        failed,
        errored,
        failedTotal: failed + errored,
        pct: total ? (passed / total) * 100 : 0,
        cost: costSamples.length ? cost : null,
        costP50: percentile(costSamples, 0.5),
        costP90: percentile(costSamples, 0.9),
        duration: durSamples.length ? durationSum : null,
        durationP50: percentile(durSamples, 0.5),
        durationP90: percentile(durSamples, 0.9),
    };
}

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
    activation,
    reviewsByTask,
    reviewTagCounts,
}: {
    runId: string;
    tasks: TaskResultSummary[];
    // Run-level activation rollup; null on runs without an activation suite, in
    // which case the metrics grid has one fewer card.
    activation?: ActivationScore | null;
    reviewsByTask?: Map<string, ReviewIndexEntry>;
    reviewTagCounts?: { tag: string; count: number }[];
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();

    const selectedTags = useMemo(
        () => parseTagsParam(searchParams.get("tags")),
        [searchParams],
    );
    const selectedSet = useMemo(() => new Set(selectedTags), [selectedTags]);

    const selectedReviewTags = useMemo(
        () => parseTagsParam(searchParams.get("rtags")),
        [searchParams],
    );
    const selectedReviewSet = useMemo(
        () => new Set(selectedReviewTags),
        [selectedReviewTags],
    );

    const q = searchParams.get("q") ?? "";
    const [showAllTags, setShowAllTags] = useState(false);

    const updateParam = useCallback(
        (key: string, next: string[]) => {
            // Read the live URL on commit so a concurrent debounced write
            // from SearchBox doesn't clobber this update (or vice versa).
            const params = new URLSearchParams(window.location.search);
            if (next.length === 0) params.delete(key);
            else params.set(key, next.join(","));
            const qs = params.toString();
            router.replace(qs ? `${pathname}?${qs}` : pathname, {
                scroll: false,
            });
        },
        [pathname, router],
    );

    const toggleTag = useCallback(
        (tag: string) => {
            updateParam(
                "tags",
                selectedSet.has(tag)
                    ? selectedTags.filter((t) => t !== tag)
                    : [...selectedTags, tag],
            );
        },
        [selectedSet, selectedTags, updateParam],
    );

    const toggleReviewTag = useCallback(
        (tag: string) => {
            updateParam(
                "rtags",
                selectedReviewSet.has(tag)
                    ? selectedReviewTags.filter((t) => t !== tag)
                    : [...selectedReviewTags, tag],
            );
        },
        [selectedReviewSet, selectedReviewTags, updateParam],
    );

    const clearAll = useCallback(() => {
        const params = new URLSearchParams(window.location.search);
        params.delete("q");
        params.delete("tags");
        params.delete("rtags");
        const qs = params.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    }, [pathname, router]);

    // Skill is the primary group; everything else is a secondary tag. The
    // secondary "Tags" rail excludes the per-task skill so it doesn't echo
    // chips already shown in the Skills row above.
    const allSkills = useMemo(() => {
        const counts = new Map<string, number>();
        for (const t of tasks) {
            if (t.skill) counts.set(t.skill, (counts.get(t.skill) ?? 0) + 1);
        }
        return [...counts.entries()].sort(
            ([a, ac], [b, bc]) => bc - ac || a.localeCompare(b),
        );
    }, [tasks]);

    const allTags = useMemo(() => {
        const counts = new Map<string, number>();
        for (const t of tasks) {
            for (const tag of t.tags) {
                if (tag === t.skill) continue;
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
            // Match on either real tags or the derived skill, so the same
            // `tags` URL param works for both rails. Robust to new runs where
            // the skill comes from task_path but is missing from task.tags.
            arr = arr.filter((t) =>
                selectedTags.every(
                    (tag) => t.tags.includes(tag) || t.skill === tag,
                ),
            );
        }
        if (selectedReviewTags.length > 0) {
            arr = arr.filter((t) => {
                const rtags = reviewsByTask?.get(t.taskId)?.tags ?? [];
                return selectedReviewTags.every((tag) => rtags.includes(tag));
            });
        }
        if (qLower) {
            arr = arr.filter((t) => {
                if (t.taskId.toLowerCase().includes(qLower)) return true;
                if (
                    humanizeTaskId(t.taskId).toLowerCase().includes(qLower)
                )
                    return true;
                if (t.skill && t.skill.toLowerCase().includes(qLower))
                    return true;
                if (t.tags.some((tag) => tag.toLowerCase().includes(qLower)))
                    return true;
                const rtags = reviewsByTask?.get(t.taskId)?.tags ?? [];
                if (rtags.some((tag) => tag.toLowerCase().includes(qLower)))
                    return true;
                return false;
            });
        }
        return arr;
    }, [tasks, selectedTags, selectedReviewTags, reviewsByTask, qLower]);

    const isFiltered =
        selectedTags.length > 0 ||
        selectedReviewTags.length > 0 ||
        qLower.length > 0;

    // Filter-aware metrics computed from `filtered`.
    const metrics = useMemo(() => computeRunMetrics(filtered), [filtered]);

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

    // Skills cap is generous (20) since the universe is finite and rarely
    // exceeds that; selection stickiness still applies in case it ever does.
    const visibleSkills = useMemo(() => {
        const top = allSkills.slice(0, TOP_N_SKILLS);
        const topNames = new Set(top.map(([t]) => t));
        const sticky = allSkills.filter(
            ([t]) => selectedSet.has(t) && !topNames.has(t),
        );
        return [...top, ...sticky];
    }, [allSkills, selectedSet]);

    const emptyHint = (() => {
        const allFilterTags = [...selectedTags, ...selectedReviewTags];
        if (allFilterTags.length > 0 && !qLower) {
            return `no tasks match all selected tags (${allFilterTags.join(" + ")})`;
        }
        if (qLower && allFilterTags.length === 0) {
            return `no tasks match "${q.trim()}"`;
        }
        if (isFiltered) return "no tasks match the current filter";
        return "no tasks in this run";
    })();

    return (
        <div className="space-y-5">
            <div
                className={`grid grid-cols-2 ${
                    activation ? "md:grid-cols-6" : "md:grid-cols-5"
                } gap-3`}
            >
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
                {activation && (
                    <ActivationCard runId={runId} activation={activation} />
                )}
                <Metric
                    label="Total cost"
                    value={
                        metrics.cost != null
                            ? `$${metrics.cost.toFixed(2)}`
                            : "—"
                    }
                    sub={
                        metrics.costP50 != null && metrics.costP90 != null
                            ? `p50 $${metrics.costP50.toFixed(2)} · p90 $${metrics.costP90.toFixed(2)}`
                            : undefined
                    }
                />
                <Metric
                    label="Time"
                    value={fmtDuration(metrics.duration)}
                    sub={
                        metrics.durationP50 != null &&
                        metrics.durationP90 != null
                            ? `p50 ${fmtDuration(metrics.durationP50)} · p90 ${fmtDuration(metrics.durationP90)}`
                            : undefined
                    }
                />
            </div>

            {(allSkills.length > 0 ||
                allTags.length > 0 ||
                (reviewTagCounts && reviewTagCounts.length > 0)) && (
                <div className="space-y-1.5">
                    <ChipLegend />
                    <CollapsibleRail id="run-tagrail">
                    <div className="flex flex-wrap items-center gap-1.5">
                        {visibleSkills.map(([tag, count]) => (
                            <ChipButton
                                key={`s:${tag}`}
                                tag={tag}
                                count={count}
                                variant="skill"
                                size="md"
                                active={selectedSet.has(tag)}
                                onClick={() => toggleTag(tag)}
                            />
                        ))}
                        {(reviewTagCounts ?? []).map(({ tag, count }) => (
                            <ChipButton
                                key={`r:${tag}`}
                                tag={tag}
                                count={count}
                                variant="review"
                                size="md"
                                active={selectedReviewSet.has(tag)}
                                onClick={() => toggleReviewTag(tag)}
                            />
                        ))}
                        {visibleTags.map(([tag, count]) => (
                            <ChipButton
                                key={`t:${tag}`}
                                tag={tag}
                                count={count}
                                variant="tag"
                                size="md"
                                active={selectedSet.has(tag)}
                                onClick={() => toggleTag(tag)}
                            />
                        ))}
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
                    </CollapsibleRail>
                </div>
            )}

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
                reviewsByTask={reviewsByTask}
                reviewSelectedSet={selectedReviewSet}
                onToggleReviewTag={toggleReviewTag}
            />
        </div>
    );
}
