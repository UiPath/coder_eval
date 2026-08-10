// Per-task trend aggregations over the most recent N runs. Reuses the
// PerRun loader from overview.ts. Trends is fixed to a recency-based slice
// rather than the date-window the front page uses — the small eval cadence
// means "last 10 runs" is a more useful unit than "last 7 days".

import { unstable_cache } from "next/cache";
import {
    aggregateTaskTagCounts,
    loadRecentRuns,
    type PerRun,
    type TagCount,
} from "./overview";
import { DEFAULT_HARNESS } from "./harness";
import { taskCarriesRepoTag } from "./tags";
import type { ComponentSha } from "./runs";

export const TRENDS_RECENT_RUN_COUNT = 10;

export interface TaskTrend {
    taskId: string;
    skill: string | null;
    // Union of task-level tags observed across the runs in scope. Used by the
    // trends page to filter the table by clicked tag rail chip.
    tags: string[];
    totalRuns: number;
    successRuns: number;
    passRate: number; // 0-1
    avgDurationSeconds: number | null; // SUCCESS runs only
    avgCostUsd: number | null; // SUCCESS runs only
    avgActualCommands: number | null; // SUCCESS runs only
    avgTotalTurns: number | null; // SUCCESS runs only
    // Status sequence newest-first, one entry per run the task APPEARS in —
    // a subset of TrendsData.runIds. The view aligns these to the full run
    // axis and renders an explicit gap slot for runs without an entry.
    // `matureSkipped` flags a run that carried the task forward as a pass
    // without executing it (so the status strip can mark which slots were
    // skips). Optional so test factories predating the field stay valid.
    recentStatuses: {
        runId: string;
        status: string | null;
        matureSkipped?: boolean;
    }[];
    // How many of the runs in scope skipped this task as mature (carried forward
    // as a pass to save cost). > 0 marks the task as currently mature; the trends
    // view surfaces the count and flags which run slots were skips. Optional so
    // test factories predating the field stay valid.
    matureSkips?: number;
    // Failure-only review tags aggregated across the slice. Secondary signal —
    // surfaces dominant failure mode without crowding out the primary metrics.
    dominantFailureTags: { tag: string; count: number }[];
}

export interface TaskHistoryEntry {
    runId: string;
    status: string | null;
    durationSeconds: number | null;
    totalCostUsd: number | null;
    weightedScore: number | null;
    actualCommands: number | null;
    totalTurns: number | null;
    expectedTurns: number | null;
    hasFinalReply: boolean;
    componentShas: ComponentSha[];
    failureTags: string[];
    // True when this run skipped the task as mature and carried it forward as a
    // pass. The history row then dashes out the un-measured cost/duration/turns
    // rather than showing the carried-forward zeros as if they were real.
    matureSkipped: boolean;
}

const DOMINANT_TAG_LIMIT = 4;

function avg(nums: number[]): number | null {
    if (nums.length === 0) return null;
    return nums.reduce((a, b) => a + b, 0) / nums.length;
}

export interface TrendsData {
    // Run ids in scope, newest first — the slot axis every task's status
    // strip aligns to. Tasks added/renamed/retired mid-window appear in only
    // a subset of these runs; the view renders an explicit gap for the rest.
    // Runs whose run.json failed to load or contained no tasks are excluded:
    // they contribute no statuses to any task, so keeping them would render
    // an all-gap column for every row.
    runIds: string[];
    trends: TaskTrend[];
}

export function aggregate(perRun: PerRun[]): TrendsData {
    // Newest first so recentStatuses comes out chronological-descending.
    const sorted = [...perRun].sort((a, b) => b.id.localeCompare(a.id));
    const runIds = sorted
        // Mirrors overview.ts's isUsablePipelineRun minus the adhoc clause:
        // loadRecentRuns already strips ad-hoc runs before they reach here.
        .filter((r) => r.overview != null && r.overview.tasks.length > 0)
        .map((r) => r.id);

    type Bucket = {
        skill: string | null;
        tagSet: Set<string>;
        statuses: {
            runId: string;
            status: string | null;
            matureSkipped?: boolean;
        }[];
        durations: number[]; // success only
        costs: number[]; // success only
        tools: number[]; // success only — actualCommands per run
        totalTurns: number[]; // success only
        successCount: number;
        totalCount: number;
        matureSkips: number;
        tagCounts: Map<string, number>;
    };
    const buckets = new Map<string, Bucket>();

    for (const { id, overview, reviewTagsByTask } of sorted) {
        if (!overview) continue;
        // Trends assume one row per (run, task): each run is a single sample in a
        // task's status history / rolling-window denominator. A repeated task has
        // multiple rows in one run, so collapse to the first occurrence here —
        // otherwise a 3×-repeated task would weight 3× in its pass-rate trend.
        //
        // NOTE: this is intentionally a *first-occurrence* sample, NOT the
        // any-replicate-passed rollup the run page's Pass-rate tile uses
        // (see run-view.tsx / perTaskPassCounts). Trends track one representative
        // status per run over time; the run tile answers "did the task pass at
        // all this run". The two can differ for a mixed-replicate run by design.
        const seenThisRun = new Set<string>();
        for (const t of overview.tasks) {
            if (seenThisRun.has(t.taskId)) continue;
            seenThisRun.add(t.taskId);
            let b = buckets.get(t.taskId);
            if (!b) {
                b = {
                    skill: t.skill,
                    tagSet: new Set(),
                    statuses: [],
                    durations: [],
                    costs: [],
                    tools: [],
                    totalTurns: [],
                    successCount: 0,
                    totalCount: 0,
                    matureSkips: 0,
                    tagCounts: new Map(),
                };
                buckets.set(t.taskId, b);
            }
            if (!b.skill && t.skill) b.skill = t.skill;
            for (const tg of t.tags) b.tagSet.add(tg);
            b.totalCount += 1;
            if (t.matureSkipped) b.matureSkips += 1;
            b.statuses.push({
                runId: id,
                status: t.status,
                matureSkipped: t.matureSkipped ?? false,
            });
            if (t.status === "SUCCESS") {
                b.successCount += 1;
                // A mature task the nightly skipped still counts as a pass, but
                // it wasn't executed — its row carries 0 cost / 0 duration and
                // no turns. Folding those zeros into the averages would make the
                // task look like it ran for free, so skip the metric pushes
                // (pass rate above is unaffected).
                if (!t.matureSkipped) {
                    if (t.durationSeconds != null) b.durations.push(t.durationSeconds);
                    if (t.totalCostUsd != null) b.costs.push(t.totalCostUsd);
                    if (t.actualCommands != null) b.tools.push(t.actualCommands);
                    if (t.totalTurns != null) b.totalTurns.push(t.totalTurns);
                }
            }
            const failTags = reviewTagsByTask[t.taskId];
            if (failTags) {
                for (const tag of failTags) {
                    b.tagCounts.set(tag, (b.tagCounts.get(tag) ?? 0) + 1);
                }
            }
        }
    }

    const trends: TaskTrend[] = [];
    for (const [taskId, b] of buckets) {
        trends.push({
            taskId,
            skill: b.skill,
            tags: [...b.tagSet],
            totalRuns: b.totalCount,
            successRuns: b.successCount,
            passRate: b.totalCount > 0 ? b.successCount / b.totalCount : 0,
            avgDurationSeconds: avg(b.durations),
            avgCostUsd: avg(b.costs),
            avgActualCommands: avg(b.tools),
            avgTotalTurns: avg(b.totalTurns),
            recentStatuses: b.statuses,
            matureSkips: b.matureSkips,
            dominantFailureTags: [...b.tagCounts.entries()]
                .map(([tag, count]) => ({ tag, count }))
                .sort(
                    (a, b) =>
                        b.count - a.count || a.tag.localeCompare(b.tag),
                )
                .slice(0, DOMINANT_TAG_LIMIT),
        });
    }

    // Default sort: lowest pass rate first (worst offenders up top), then by
    // total run count desc, then taskId for determinism.
    trends.sort(
        (a, b) =>
            a.passRate - b.passRate ||
            b.totalRuns - a.totalRuns ||
            a.taskId.localeCompare(b.taskId),
    );
    return { runIds, trends };
}

// TrendsData plus the tag-rail counts, both derived from the SAME loaded
// window. Folding the counts in here means the trends page loads the recent-run
// window once (via this cached aggregate) instead of a second uncached
// loadRecentRuns call just for the rail.
export interface TrendsPageData extends TrendsData {
    tagCounts: {
        skills: TagCount[];
        taskTags: TagCount[];
        reviewTags: TagCount[];
    };
}

async function aggregateTaskTrendsInner(
    limit: number,
    harness: string,
): Promise<TrendsPageData> {
    const perRun = await loadRecentRuns(limit, harness);
    return { ...aggregate(perRun), tagCounts: aggregateTaskTagCounts(perRun) };
}

const cachedAggregate = unstable_cache(
    aggregateTaskTrendsInner,
    // v3: the cached shape gained tagCounts and the args gained `harness` — the
    // key bump keeps a stale pre-deploy payload from being served into new code.
    // (harness is part of the auto-generated key too, so each harness caches
    // independently.)
    ["aggregate-task-trends-v3"],
    { revalidate: 300 },
);

export function aggregateTaskTrends(
    limit: number = TRENDS_RECENT_RUN_COUNT,
    harness: string = DEFAULT_HARNESS,
): Promise<TrendsPageData> {
    return cachedAggregate(limit, harness);
}

// Predicate matching getOverview's tag scoping logic, but operating on the
// aggregated TaskTrend (we don't keep per-run reviewTagsByTask around after
// aggregation; dominantFailureTags is the equivalent task-level signal).
//
// NOTE — divergence from /path-to-ga, which answers the same question under
// stricter rules. `trend.tags` is a UNION over the window, so a task de-tagged
// upstream keeps matching here until every run that predates the removal ages
// out; lib/overview.ts::buildTagTaskRows instead drops a task proven de-tagged
// by a newer run. Likewise `passRate` below counts mature carry-forwards as
// passes, which that page excludes. Both are deliberate: this is the general
// browsing surface, that one is a GA-readiness report.
export function trendMatchesTag(trend: TaskTrend, tag: string): boolean {
    if (taskCarriesRepoTag(trend, tag)) return true;
    return trend.dominantFailureTags.some((t) => t.tag === tag);
}

export async function historyForTaskInner(
    taskId: string,
    limit: number,
    harness: string = DEFAULT_HARNESS,
): Promise<TaskHistoryEntry[]> {
    const perRun = await loadRecentRuns(limit, harness);
    const out: TaskHistoryEntry[] = [];
    for (const { id, overview, reviewTagsByTask } of perRun) {
        if (!overview) continue;
        const t = overview.tasks.find((x) => x.taskId === taskId);
        if (!t) continue;
        out.push({
            runId: id,
            status: t.status,
            durationSeconds: t.durationSeconds,
            totalCostUsd: t.totalCostUsd,
            weightedScore: t.weightedScore,
            actualCommands: t.actualCommands,
            totalTurns: t.totalTurns,
            expectedTurns: t.expectedTurns,
            hasFinalReply: t.hasFinalReply,
            componentShas: overview.componentShas,
            failureTags: reviewTagsByTask[taskId] ?? [],
            matureSkipped: t.matureSkipped ?? false,
        });
    }
    out.sort((a, b) => b.runId.localeCompare(a.runId));
    return out;
}

const cachedHistory = unstable_cache(
    historyForTaskInner,
    // v2: gained the `harness` arg (auto-keyed), so a task's history caches
    // per harness rather than serving one harness's rows under another.
    ["history-for-task-v2"],
    { revalidate: 300 },
);

export function historyForTask(
    taskId: string,
    limit: number = TRENDS_RECENT_RUN_COUNT,
    harness: string = DEFAULT_HARNESS,
): Promise<TaskHistoryEntry[]> {
    return cachedHistory(taskId, limit, harness);
}
