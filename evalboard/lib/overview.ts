// Front-page aggregations: daily success rate chart, tag rails, and the
// recent-runs listing. All three share a single windowed fetch — runs in
// the window are read once and projected into multiple shapes downstream.

import { unstable_cache } from "next/cache";
import {
    listRunIds,
    readRunOverview,
    type RunOverview,
    type RunOverviewTask,
} from "./runs";
import { listRunIdsInWindow, readRunReviewIndex, parseRunIdDate } from "./reviews";
import { humanizeTaskId } from "./format";
import { mapWithConcurrency } from "./concurrency";
import type { Window } from "./reviews-types";

export interface RunPoint {
    runId: string;
    timestamp: number; // ms since epoch (UTC); used as the chart x-coordinate
    successRate: number | null;
}

export interface TagCount {
    tag: string;
    count: number;
}

export interface OverviewData {
    runs: RunPoint[]; // one point per run, no daily aggregation
    windowStart: number; // ms — chart x-domain start
    windowEnd: number; // ms — chart x-domain end
    skills: TagCount[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
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

const WINDOW_DAYS: Record<Window, number> = {
    "1d": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
};

// Projected per-run snapshot: only the fields needed downstream. Keeps the
// cached payload small (the raw ReviewIndex can be MBs for busy runs).
// Shape is plain serializable JSON — unstable_cache stringifies its values,
// so Map/Set won't round-trip.
export interface PerRun {
    id: string;
    overview: RunOverview | null;
    // tag -> count of review entries carrying it (for the rose rail).
    reviewTagCounts: Record<string, number>;
    // taskId -> deduped list of review tags (for task-level filter matching).
    reviewTagsByTask: Record<string, string[]>;
}

async function loadPerRunForId(id: string): Promise<PerRun> {
    // readRunOverview / readRunReviewIndex swallow 404s and JSON parse errors,
    // but ensureRunSummary (called underneath) re-throws transient auth / IMDS /
    // 5xx errors. A single bad run must not tank the whole page — downgrade to
    // a null-overview PerRun so the aggregators (which already skip nulls) can
    // proceed with the other runs.
    let overview: RunOverview | null = null;
    let reviewIndex: Awaited<ReturnType<typeof readRunReviewIndex>> = null;
    try {
        [overview, reviewIndex] = await Promise.all([
            readRunOverview(id),
            readRunReviewIndex(id),
        ]);
    } catch (err) {
        console.error(`[evalboard] loadPerRunForId(${id}) failed:`, err);
        return { id, overview: null, reviewTagCounts: {}, reviewTagsByTask: {} };
    }
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
}

async function loadWindowDataInner(window: Window): Promise<PerRun[]> {
    const ids = await listRunIdsInWindow(window);
    return mapWithConcurrency(ids, FETCH_CONCURRENCY, loadPerRunForId);
}

async function loadRecentRunsInner(limit: number): Promise<PerRun[]> {
    const ids = (await listRunIds()).slice(0, limit);
    return mapWithConcurrency(ids, FETCH_CONCURRENCY, loadPerRunForId);
}

const cachedLoadRecentRuns = unstable_cache(
    loadRecentRunsInner,
    ["evalboard-recent-runs"],
    { revalidate: 300 },
);

// Fetch the N most recent runs in PerRun shape. Bypasses the
// window-keyed cache used by getOverview because the trends page is
// fixed-count, not date-bounded.
export function loadRecentRuns(limit: number): Promise<PerRun[]> {
    return cachedLoadRecentRuns(limit);
}

// Tag-count aggregation where each tag's count is the number of distinct
// TASKS carrying it across the slice — mirrors the per-task counting used
// by the run-detail page. Use this for views that aggregate across runs but
// want to surface "how many tasks does this tag describe" rather than
// "how many runs include any task with this tag".
export function aggregateTaskTagCounts(perRun: PerRun[]): {
    skills: TagCount[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
} {
    const skillTaskIds = new Map<string, Set<string>>();
    const taskTagTaskIds = new Map<string, Set<string>>();
    const reviewTagTaskIds = new Map<string, Set<string>>();

    function add(m: Map<string, Set<string>>, key: string, taskId: string) {
        let s = m.get(key);
        if (!s) {
            s = new Set();
            m.set(key, s);
        }
        s.add(taskId);
    }

    for (const { overview, reviewTagsByTask } of perRun) {
        if (overview) {
            for (const t of overview.tasks) {
                if (t.skill) add(skillTaskIds, t.skill, t.taskId);
                for (const tg of t.tags) {
                    if (tg === t.skill) continue;
                    add(taskTagTaskIds, tg, t.taskId);
                }
            }
        }
        for (const [taskId, tags] of Object.entries(reviewTagsByTask)) {
            for (const tag of tags) {
                add(reviewTagTaskIds, tag, taskId);
            }
        }
    }

    function toCounts(m: Map<string, Set<string>>): TagCount[] {
        return [...m.entries()]
            .map(([tag, ids]) => ({ tag, count: ids.size }))
            .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
    }

    return {
        skills: toCounts(skillTaskIds),
        taskTags: toCounts(taskTagTaskIds),
        reviewTags: toCounts(reviewTagTaskIds),
    };
}

// Tag rail aggregation. Counts == "runs containing the tag", not total
// occurrences — one increment per (tag, run) pair.
function aggregateTagCounts(perRun: PerRun[]): {
    skills: TagCount[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
} {
    const skillRunIds = new Map<string, Set<string>>();
    const taskTagRunIds = new Map<string, Set<string>>();
    const reviewTagRunIds = new Map<string, Set<string>>();

    function add(m: Map<string, Set<string>>, key: string, runId: string) {
        let s = m.get(key);
        if (!s) {
            s = new Set();
            m.set(key, s);
        }
        s.add(runId);
    }

    for (const { id, overview, reviewTagsByTask } of perRun) {
        if (overview) {
            for (const t of overview.tasks) {
                if (t.skill) add(skillRunIds, t.skill, id);
                for (const tg of t.tags) {
                    if (tg === t.skill) continue;
                    add(taskTagRunIds, tg, id);
                }
            }
        }
        for (const tags of Object.values(reviewTagsByTask)) {
            for (const tag of tags) {
                add(reviewTagRunIds, tag, id);
            }
        }
    }

    function toCounts(m: Map<string, Set<string>>): TagCount[] {
        return [...m.entries()]
            .map(([tag, ids]) => ({ tag, count: ids.size }))
            .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
    }

    return {
        skills: toCounts(skillRunIds),
        taskTags: toCounts(taskTagRunIds),
        reviewTags: toCounts(reviewTagRunIds),
    };
}


// Module-scope cache wrapper. Keying on the window arg means a single
// request (Promise.all over getOverview+getRunListing) shares one fetch,
// and cross-request results live for 5 minutes.
const loadWindowData = unstable_cache(
    loadWindowDataInner,
    ["evalboard-window-data"],
    { revalidate: 300 },
);

export function taskMatchesTag(
    task: RunOverviewTask,
    reviewTagsByTask: Record<string, string[]>,
    tag: string,
): boolean {
    if (task.skill === tag) return true;
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
    if (task.skill && task.skill.toLowerCase().includes(needle)) return true;
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

    // ---- Per-run chart points ----
    // One point per run plotted at its own timestamp, no daily averaging.
    // When tag or q is active, scope each run's rate to only matching tasks.
    const runPoints: RunPoint[] = [];

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
        runPoints.push({
            runId: id,
            timestamp: date.getTime(),
            successRate: rate,
        });
    }
    runPoints.sort((a, b) => a.timestamp - b.timestamp);

    const nowMs = Date.now();
    const windowStart = nowMs - WINDOW_DAYS[window] * 86400_000;
    const windowEnd = nowMs;

    // Tag rail counts are over the full window, ignoring the active filter —
    // users need to see other tags they could switch to. Runs-based counting
    // (one increment per (tag, run) pair) is the front-page convention.
    const { skills, taskTags, reviewTags } = aggregateTagCounts(perRun);

    return {
        runs: runPoints,
        windowStart,
        windowEnd,
        skills,
        taskTags,
        reviewTags,
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
