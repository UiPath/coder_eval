// Front-page aggregations: daily success rate chart, tag rails, and the
// recent-runs listing. All three share a single windowed fetch — runs in
// the window are read once and projected into multiple shapes downstream.

import { unstable_cache } from "next/cache";
import {
    listRunIds,
    readRunMeta,
    readRunOverview,
    type RunMeta,
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
    // From the optional meta.json sidecar. Both optional so callers/tests that
    // build a PerRun without metadata stay valid; absent === non-ad-hoc.
    adhoc?: boolean;
    title?: string | null;
}

async function loadPerRunForId(id: string): Promise<PerRun> {
    // readRunOverview / readRunReviewIndex swallow 404s and JSON parse errors,
    // but ensureRunSummary (called underneath) re-throws transient auth / IMDS /
    // 5xx errors. A single bad run must not tank the whole page — downgrade to
    // a null-overview PerRun so the aggregators (which already skip nulls) can
    // proceed with the other runs.
    let overview: RunOverview | null = null;
    let reviewIndex: Awaited<ReturnType<typeof readRunReviewIndex>> = null;
    let meta: RunMeta | null = null;
    try {
        [overview, reviewIndex, meta] = await Promise.all([
            readRunOverview(id),
            readRunReviewIndex(id),
            readRunMeta(id),
        ]);
    } catch (err) {
        console.error(`[evalboard] loadPerRunForId(${id}) failed:`, err);
        return {
            id,
            overview: null,
            reviewTagCounts: {},
            reviewTagsByTask: {},
            adhoc: false,
            title: null,
        };
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
    return {
        id,
        overview,
        reviewTagCounts,
        reviewTagsByTask,
        adhoc: meta?.adhoc === true,
        title: meta?.title ?? null,
    };
}

// Cache at per-run granularity, NOT per-window. A whole-window PerRun[] for
// the 30d window exceeds unstable_cache's hard 2MB ceiling (≈400 tasks × ~20
// runs), and on overflow Next.js drops the write AND hands back a truncated
// payload — silently shearing off the newest runs (the 30d front page lost
// "today"). One run's projection is well under 2MB, so keying the cache on the
// run id keeps every entry cacheable and lets entries be reused across all
// windows + the trends page. The cross-run aggregation downstream is cheap
// in-memory work, so leaving it uncached costs nothing.
const cachedLoadPerRun = unstable_cache(loadPerRunForId, ["evalboard-per-run"], {
    revalidate: 300,
});

async function loadWindowDataInner(window: Window): Promise<PerRun[]> {
    const ids = await listRunIdsInWindow(window);
    return mapWithConcurrency(ids, FETCH_CONCURRENCY, cachedLoadPerRun);
}

// Fetch the N most recent runs in PerRun shape. Recency-based (fixed count)
// rather than date-bounded — used by the trends page.
export function loadRecentRuns(limit: number): Promise<PerRun[]> {
    return loadRecentRunsInner(limit);
}

async function loadRecentRunsInner(limit: number): Promise<PerRun[]> {
    // Trends is the daily-cadence view: only pipeline runs belong here. Prune
    // to date-shaped ids BEFORE slicing (cheap, no IO) so ad-hoc runs — whose
    // ids sort lexically above every `2026-…` daily id and would otherwise
    // crowd out the real "recent N" — never occupy a slot. Then drop any
    // date-named run explicitly flagged adhoc (rare edge case) post-load.
    const ids = (await listRunIds())
        .filter((id) => parseRunIdDate(id) != null)
        .slice(0, limit);
    const runs = await mapWithConcurrency(ids, FETCH_CONCURRENCY, cachedLoadPerRun);
    return runs.filter((r) => !r.adhoc);
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


// Per-window assembly from the per-run cache. The expensive blob reads are
// memoized per run inside cachedLoadPerRun; gathering them for a window is
// cheap, so this stays uncached (and avoids the 2MB whole-window cache cap).
function loadWindowData(window: Window): Promise<PerRun[]> {
    return loadWindowDataInner(window);
}

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
    // Ad-hoc runs never feed the daily chart or the tag rails — they're not
    // pipeline cadence. (Non-date-named ones are already pruned upstream by
    // listRunIdsInWindow; this also drops date-named runs flagged adhoc.)
    const perRun = (await loadWindowData(window)).filter((r) => !r.adhoc);
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
    // Exclude ad-hoc runs from the main listing — they appear in their own
    // section (getAdhocRunListing). totalInWindow therefore counts only
    // pipeline runs, matching the chart above it.
    const perRun = (await loadWindowData(window)).filter((r) => !r.adhoc);
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

export interface AdhocRunRow extends RunListingRow {
    // From meta.json; null when the run predates the feature (then the UI
    // falls back to the run id).
    title: string | null;
}

// The Ad-hoc runs section (front page, below the daily listing). "Ad-hoc"
// here means "not a daily-pipeline run" — i.e. the id isn't date-shaped, which
// is exactly the set listRunIdsInWindow excludes from the chart and main table.
// Bounded to the most recent `limit` candidates: the date-shape check is free
// (no IO), and only the shown slice is loaded. Runs without a run.json (e.g.
// aborted uploads that left only default/) are skipped.
export async function getAdhocRunListing(limit: number): Promise<AdhocRunRow[]> {
    const ids = (await listRunIds())
        .filter((id) => parseRunIdDate(id) == null)
        .slice(0, limit);
    const perRun = await mapWithConcurrency(ids, FETCH_CONCURRENCY, cachedLoadPerRun);
    const rows: AdhocRunRow[] = [];
    for (const { id, overview, title } of perRun) {
        if (!overview) continue;
        rows.push({
            id,
            title: title ?? null,
            tasksSucceeded: overview.tasks.filter((t) => t.status === "SUCCESS")
                .length,
            tasksRun: overview.tasks.length,
            totalCostUsd: overview.totalCostUsd,
            taskDurationSeconds: overview.taskDurationSeconds,
        });
    }
    return rows;
}
