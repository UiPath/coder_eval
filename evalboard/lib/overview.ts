// Front-page aggregations: daily success rate chart, tag rails, and the
// recent-runs listing. All three share a single windowed fetch — runs in
// the window are read once and projected into multiple shapes downstream.

import { unstable_cache } from "next/cache";
import { readRunOverview, type RunOverview, type RunOverviewTask } from "./runs";
import { listRunIdsInWindow, readRunReviewIndex, parseRunIdDate } from "./reviews";
import { humanizeTaskId } from "./format";
import { mapWithConcurrency } from "./concurrency";
import type { Window } from "./reviews-types";

export interface DailyPoint {
    date: string; // YYYY-MM-DD (UTC)
    avgSuccessRate: number | null;
    runCount: number;
}

export interface TagCount {
    tag: string;
    count: number;
}

export interface OverviewData {
    daily: DailyPoint[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
    runCount: number; // runs contributing to the chart in the window
    activeTag: string | null;
}

export interface RunListingRow {
    id: string;
    // When a tag/q filter is active, every metric is scoped to matching tasks.
    // Unfiltered, they are whole-run totals.
    tasksSucceeded: number;
    tasksRun: number;
    totalCostUsd: number | null;
    taskDurationSeconds: number | null;
}

export interface RunListing {
    rows: RunListingRow[]; // after filter + limit, newest first
    totalInWindow: number;
    matchedCount: number; // post-filter, pre-limit
}

const FETCH_CONCURRENCY = 16;

function toUtcDateKey(d: Date): string {
    const y = d.getUTCFullYear();
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

function* dateRange(start: Date, end: Date): Generator<string> {
    const cur = new Date(
        Date.UTC(
            start.getUTCFullYear(),
            start.getUTCMonth(),
            start.getUTCDate(),
        ),
    );
    const last = new Date(
        Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()),
    );
    while (cur <= last) {
        yield toUtcDateKey(cur);
        cur.setUTCDate(cur.getUTCDate() + 1);
    }
}

const WINDOW_DAYS: Record<Window, number> = {
    "1d": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
};

function sortTagCounts(m: Map<string, number>): TagCount[] {
    return [...m.entries()]
        .map(([tag, count]) => ({ tag, count }))
        .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

// Projected per-run snapshot: only the fields needed downstream. Keeps the
// cached payload small (the raw ReviewIndex can be MBs for busy runs).
// Shape is plain serializable JSON — unstable_cache stringifies its values,
// so Map/Set won't round-trip.
interface PerRun {
    id: string;
    overview: RunOverview | null;
    // tag -> count of review entries carrying it (for the rose rail).
    reviewTagCounts: Record<string, number>;
    // taskId -> deduped list of review tags (for task-level filter matching).
    reviewTagsByTask: Record<string, string[]>;
}

async function loadWindowDataInner(window: Window): Promise<PerRun[]> {
    const ids = await listRunIdsInWindow(window);
    return mapWithConcurrency(ids, FETCH_CONCURRENCY, async (id) => {
        const [overview, reviewIndex] = await Promise.all([
            readRunOverview(id),
            readRunReviewIndex(id),
        ]);
        const reviewTagCounts: Record<string, number> = {};
        const tagSetByTask = new Map<string, Set<string>>();
        if (reviewIndex) {
            for (const e of reviewIndex.reviews) {
                let s = tagSetByTask.get(e.task_id);
                if (!s) {
                    s = new Set();
                    tagSetByTask.set(e.task_id, s);
                }
                for (const tag of e.tags) {
                    s.add(tag);
                    reviewTagCounts[tag] = (reviewTagCounts[tag] ?? 0) + 1;
                }
            }
        }
        const reviewTagsByTask: Record<string, string[]> = {};
        for (const [taskId, tags] of tagSetByTask) {
            reviewTagsByTask[taskId] = [...tags];
        }
        return { id, overview, reviewTagCounts, reviewTagsByTask };
    });
}

// Module-scope cache wrapper. Keying on the window arg means a single
// request (Promise.all over getOverview+getRunListing) shares one fetch,
// and cross-request results live for 5 minutes.
const cachedLoadWindowData = unstable_cache(
    loadWindowDataInner,
    ["evalboard-window-data"],
    { revalidate: 300 },
);

function loadWindowData(window: Window): Promise<PerRun[]> {
    return cachedLoadWindowData(window);
}

function taskMatchesTag(
    task: RunOverviewTask,
    reviewTagsByTask: Record<string, string[]>,
    tag: string,
): boolean {
    if (task.tags.includes(tag)) return true;
    const rt = reviewTagsByTask[task.taskId];
    return rt ? rt.includes(tag) : false;
}

function taskMatchesQuery(
    task: RunOverviewTask,
    reviewTagsByTask: Record<string, string[]>,
    needle: string,
): boolean {
    if (task.taskId.toLowerCase().includes(needle)) return true;
    if (humanizeTaskId(task.taskId).toLowerCase().includes(needle)) return true;
    if (task.tags.some((tag) => tag.toLowerCase().includes(needle))) return true;
    const rt = reviewTagsByTask[task.taskId];
    if (rt) {
        for (const tag of rt) {
            if (tag.toLowerCase().includes(needle)) return true;
        }
    }
    return false;
}

// ---------- Public API ----------

export async function getOverview(
    window: Window,
    tag: string | null = null,
    q: string | null = null,
): Promise<OverviewData> {
    const perRun = await loadWindowData(window);
    const needle = q?.trim().toLowerCase() || null;

    // ---- Daily bucketing ----
    // Per-day average of per-run success rates (matches ADX "avg_success_rate").
    // When tag or q is active, scope each run's rate to only matching tasks.
    type DayBucket = { rateSum: number; rateCount: number };
    const byDay = new Map<string, DayBucket>();
    let contributingRuns = 0;

    for (const { id, overview, reviewTagsByTask } of perRun) {
        if (!overview || overview.tasks.length === 0) continue;
        const date = parseRunIdDate(id);
        if (!date) continue;

        let matching = overview.tasks;
        if (tag) {
            matching = matching.filter((t) =>
                taskMatchesTag(t, reviewTagsByTask, tag),
            );
        }
        if (needle) {
            matching = matching.filter((t) =>
                taskMatchesQuery(t, reviewTagsByTask, needle),
            );
        }
        if (matching.length === 0) continue;

        const succeeded = matching.filter(
            (t) => t.status === "SUCCESS",
        ).length;
        const rate = (succeeded / matching.length) * 100;

        const key = toUtcDateKey(date);
        const b = byDay.get(key) ?? { rateSum: 0, rateCount: 0 };
        b.rateSum += rate;
        b.rateCount += 1;
        byDay.set(key, b);
        contributingRuns += 1;
    }

    const now = new Date();
    const start = new Date(now.getTime() - WINDOW_DAYS[window] * 86400_000);
    const daily: DailyPoint[] = [];
    for (const key of dateRange(start, now)) {
        const b = byDay.get(key);
        daily.push({
            date: key,
            avgSuccessRate: b ? b.rateSum / b.rateCount : null,
            runCount: b ? b.rateCount : 0,
        });
    }

    // ---- Tag aggregation (over full window, regardless of filter) ----
    const taskTagCounts = new Map<string, number>();
    const reviewTagCounts = new Map<string, number>();

    for (const { overview, reviewTagCounts: rtc } of perRun) {
        if (overview) {
            for (const t of overview.tasks) {
                for (const tg of t.tags) {
                    taskTagCounts.set(tg, (taskTagCounts.get(tg) ?? 0) + 1);
                }
            }
        }
        for (const [tg, c] of Object.entries(rtc)) {
            reviewTagCounts.set(tg, (reviewTagCounts.get(tg) ?? 0) + c);
        }
    }

    return {
        daily,
        taskTags: sortTagCounts(taskTagCounts),
        reviewTags: sortTagCounts(reviewTagCounts),
        runCount: contributingRuns,
        activeTag: tag,
    };
}

export async function getRunListing(
    window: Window,
    tag: string | null,
    q: string | null,
    limit: number | null, // null = unlimited
): Promise<RunListing> {
    const perRun = await loadWindowData(window);
    // Run IDs are timestamped — newest first by lexical compare.
    const sorted = [...perRun].sort((a, b) => b.id.localeCompare(a.id));
    const totalInWindow = sorted.length;

    const needle = q?.trim().toLowerCase() || null;
    const needsFilter = tag != null || needle != null;

    const matched: RunListingRow[] = [];
    for (const { id, overview, reviewTagsByTask } of sorted) {
        if (!overview) continue;

        // Default to the whole-run slice; narrow to matching tasks if a
        // filter is active AND any task matches. When `q` matches only the
        // run ID (date-fragment "pin a run" use case), we keep the whole-run
        // slice so the row shows real totals rather than 0/—/—.
        let scopedTasks = overview.tasks;
        let scopedCost = overview.totalCostUsd;
        let scopedDur = overview.taskDurationSeconds;

        if (needsFilter) {
            const matching = overview.tasks.filter((t) => {
                const passesTag =
                    tag == null || taskMatchesTag(t, reviewTagsByTask, tag);
                const passesQ =
                    needle == null ||
                    taskMatchesQuery(t, reviewTagsByTask, needle);
                return passesTag && passesQ;
            });
            const idMatchesQ =
                needle != null && id.toLowerCase().includes(needle);
            if (matching.length === 0 && !idMatchesQ) continue;

            if (matching.length > 0) {
                // Scope cost/duration to matching tasks. Cost sums any task
                // with a recorded value; duration is only meaningful when
                // every matching task has a duration recorded (otherwise the
                // partial sum would understate the slice — mirrors
                // readRunOverview's whole-run rule).
                let costSum = 0;
                let costHasAny = false;
                let durSum = 0;
                let durAllPresent = true;
                for (const t of matching) {
                    if (t.totalCostUsd != null) {
                        costSum += t.totalCostUsd;
                        costHasAny = true;
                    }
                    if (t.durationSeconds != null) {
                        durSum += t.durationSeconds;
                    } else {
                        durAllPresent = false;
                    }
                }
                scopedTasks = matching;
                scopedCost = costHasAny ? costSum : null;
                scopedDur = durAllPresent ? durSum : null;
            }
        }

        matched.push({
            id,
            tasksSucceeded: scopedTasks.filter((t) => t.status === "SUCCESS")
                .length,
            tasksRun: scopedTasks.length,
            totalCostUsd: scopedCost,
            taskDurationSeconds: scopedDur,
        });
    }

    const rows = limit == null ? matched : matched.slice(0, limit);
    return { rows, totalInWindow, matchedCount: matched.length };
}
