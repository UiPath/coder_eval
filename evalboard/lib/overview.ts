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
import { withinTurnBudget } from "./turns";
import { humanizeTaskId } from "./format";
import { mapWithConcurrency } from "./concurrency";
import { DEFAULT_HARNESS, normalizeHarness, orderHarnesses } from "./harness";
import type { Window } from "./reviews-types";

export interface RunPoint {
    runId: string;
    timestamp: number; // ms since epoch (UTC); used as the chart x-coordinate
    // The harness this run ran on, normalized (legacy runs fold to claude-code).
    // Every run belongs to exactly one harness, so the charts split the points
    // into one series per harness and color each by identity.
    harness: string;
    successRate: number | null;
    // % of budgeted tasks whose visible turns stayed within 1.5× their
    // expected_turns budget. Only tasks carrying a positive expected_turns
    // budget are eligible — both SUCCESS and non-SUCCESS. A budgeted task that
    // did not succeed counts as over budget (a failed run never "stayed within
    // budget"); a budgeted SUCCESS task is over budget only if its visible
    // turns exceed the tolerance. Tasks with no budget are excluded regardless
    // of outcome, so the rate is stable under tag/q filtering. null when no
    // task in scope carries a budget at all (the chart shows a gap rather than
    // a failure-driven 0%). Same tag/q scoping as successRate.
    turnBudgetRate: number | null;
}

// The % of budgeted tasks whose visible turns stayed within 1.5× their
// expected_turns budget; null when no task in scope carries a budget.
//
// Eligibility is symmetric: a task counts iff it carries a positive
// expected_turns budget, whether or not it succeeded. A budgeted non-SUCCESS
// task is treated as having exhausted its budget (infinite turns) and counts
// against the rate — a failed run never "stayed within budget." A budgeted
// SUCCESS task counts within budget only when its visible turns are within
// tolerance (a budgeted SUCCESS with no visible-turn count can't be judged and
// is excluded). Budget-less tasks are excluded regardless of outcome, so the
// metric name stays literally true (every counted task had expected turns) and
// the rate does not shift when filtering changes which co-scoped tasks happen
// to be budgeted. Callers pass the already tag/q-scoped task list, so the rate
// inherits that scoping. Exported for unit testing.
//
// NOTE: this headline aggregate folds failure into the metric (a budgeted
// failure counts as over budget) and so diverges from the per-task "Turns"
// cell tint (turns.ts::turnRatio), which is a pure efficiency signal blind to
// pass/fail. See turnRatio's comment.
export function turnBudgetRateForTasks(tasks: RunOverviewTask[]): number | null {
    let eligible = 0;
    let withinBudget = 0;
    for (const t of tasks) {
        const hasBudget = t.expectedTurns != null && t.expectedTurns >= 1;
        if (!hasBudget) continue; // unbudgeted tasks never count, success or fail
        if (t.status !== "SUCCESS") {
            // A budgeted task that didn't succeed never stayed within budget.
            eligible += 1;
            continue;
        }
        const verdict = withinTurnBudget(t.visibleTurns, t.expectedTurns);
        if (verdict === null) continue; // budgeted SUCCESS but no turn data → can't judge
        eligible += 1;
        if (verdict) withinBudget += 1;
    }
    // No budgeted task in scope → nothing to report (not a failure-driven 0%).
    return eligible > 0 ? (withinBudget / eligible) * 100 : null;
}

export interface TagCount {
    tag: string;
    count: number;
}

export interface OverviewData {
    runs: RunPoint[]; // one point per run, no daily aggregation
    // The harnesses actually present in `runs`, in stable display order. Drives
    // the chart's series list and legend, so a harness with no runs in the
    // window contributes no empty line.
    harnesses: string[];
    windowStart: number; // ms — chart x-domain start
    windowEnd: number; // ms — chart x-domain end
    // The summary tiles' rollup, folded over the same runs `runs` plots so the
    // two can't disagree. Independent of the run table's pagination, which reads
    // back past this window (see getRunListing).
    totals: RunListingTotals;
    runCount: number; // matched pipeline runs in the window
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
    // Run-level harness (coder-eval AgentKind) for the Harness column; null on
    // legacy runs that predate the RunConfig stamp / carry no agent_config.type.
    // Optional so existing test factories stay valid.
    harness?: string | null;
}

// Window-level rollup across every matched run (pre-limit), so the front-page
// summary reflects the whole time window regardless of table pagination. Cost
// and duration are summed only over runs that recorded a value; the *Partial
// flags flag that at least one matched run lacked one, so a UI can caveat the
// sum instead of silently understating it.
export interface RunListingTotals {
    costUsd: number | null; // null when no matched run recorded a cost
    costPartial: boolean; // some matched runs had no cost (sum understates)
    tasksSucceeded: number;
    tasksRun: number;
    durationSeconds: number | null; // null when no matched run recorded a duration
    durationPartial: boolean;
}

// Sum a matched-run slice into a window rollup. Pure over RunListingRow[] so it
// unit-tests without touching the blob store; getRunListing feeds it the full
// pre-limit `matched` array.
export function summarizeListing(rows: RunListingRow[]): RunListingTotals {
    let costUsd = 0;
    let costRuns = 0;
    let tasksSucceeded = 0;
    let tasksRun = 0;
    let durationSeconds = 0;
    let durationRuns = 0;
    for (const r of rows) {
        tasksSucceeded += r.tasksSucceeded;
        tasksRun += r.tasksRun;
        if (r.totalCostUsd != null) {
            costUsd += r.totalCostUsd;
            costRuns += 1;
        }
        if (r.taskDurationSeconds != null) {
            durationSeconds += r.taskDurationSeconds;
            durationRuns += 1;
        }
    }
    return {
        costUsd: costRuns > 0 ? costUsd : null,
        costPartial: costRuns > 0 && costRuns < rows.length,
        tasksSucceeded,
        tasksRun,
        durationSeconds: durationRuns > 0 ? durationSeconds : null,
        durationPartial: durationRuns > 0 && durationRuns < rows.length,
    };
}

export interface RunListing {
    rows: RunListingRow[]; // after filter + limit, newest first
    // Every pipeline run in the store, not just the ones loaded for this page.
    // Free to compute (it's the id count), so the table can say "20 of 94"
    // without reading 94 run.json files.
    totalCandidates: number;
    // Another page exists behind `rows`. Derived from over-fetching by one
    // rather than from a total match count: counting matches would mean loading
    // every run in history on every render.
    hasMore: boolean;
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
// rather than date-bounded — used by the trends page. When `harness` is set,
// only runs of that harness count toward N (the nightly now rotates
// claude-code / codex / antigravity, and a trend is only meaningful within one
// harness — mixing them blends incomparable pass rates and cost profiles).
export function loadRecentRuns(
    limit: number,
    harness?: string,
): Promise<PerRun[]> {
    return loadRecentRunsInner(limit, harness);
}

async function loadRecentRunsInner(
    limit: number,
    harness?: string,
): Promise<PerRun[]> {
    // Trends is the daily-cadence view: only pipeline runs belong here. Prune
    // to date-shaped ids BEFORE slicing (cheap, no IO) so ad-hoc runs — whose
    // ids sort lexically above every `2026-…` daily id and would otherwise
    // crowd out the real "recent N" — never occupy a slot.
    const ids = (await listRunIds()).filter((id) => parseRunIdDate(id) != null);
    const matchesHarness = harness
        ? (r: PerRun) => normalizeHarness(r.overview?.harness) === harness
        : undefined;
    return collectPipelineRuns(ids, limit, cachedLoadPerRun, matchesHarness);
}

// How many recent runs to scan when discovering which harnesses are active.
// All harnesses that run at least weekly appear within a window this size, so
// the switcher lists them without a hardcoded set (a new harness like
// "delegate" shows up on its own).
const HARNESS_DISCOVERY_COUNT = 12;

async function listRecentHarnessesInner(): Promise<string[]> {
    const perRun = await loadRecentRuns(HARNESS_DISCOVERY_COUNT);
    const seen = new Set<string>();
    for (const r of perRun) {
        if (r.overview) seen.add(normalizeHarness(r.overview.harness));
    }
    const ordered = orderHarnesses(seen);
    // Never hand back an empty list — the default must always be selectable.
    return ordered.length > 0 ? ordered : [DEFAULT_HARNESS];
}

// The harnesses present in recent runs, ordered for the switcher. Cached (and
// shares the per-run cache with the aggregates), revalidated every 5 min.
export const listRecentHarnesses = unstable_cache(
    listRecentHarnessesInner,
    ["recent-harnesses-v1"],
    { revalidate: 300 },
);

// A loaded run occupies a window slot only when it's usable downstream:
// pipeline (non-adhoc) AND has a readable overview with at least one task.
// Ad-hoc uploads, transiently unreadable runs (loadPerRunForId downgrades
// those to overview:null), and empty/aborted uploads are all skipped and
// backfilled from older candidates — none of them can silently shrink the
// window. Every consumer of loadRecentRuns already skips null overviews, so
// dropping them here only deepens the window, never changes row semantics.
function isUsablePipelineRun(r: PerRun): boolean {
    return !r.adhoc && r.overview != null && r.overview.tasks.length > 0;
}

// Backfill bounds. The scan cap keeps a pathological stretch of unusable
// runs (bulk ad-hoc uploads, or a blob outage that fails every load — and
// failures are cached for 5 min) from walking the entire container; 3× the
// window is generous for realistic density. The batch floor keeps the
// deficit-sized rounds from degenerating into one-id-per-round serial loads
// when a single slot stays unfilled.
const RECENT_SCAN_FACTOR = 3;
// When filtering to a single harness, that harness holds only a fraction of the
// recent runs (codex/antigravity run a few times a week vs. claude-code daily),
// so the scan has to reach further back to gather `limit` of them. A larger cap
// keeps a rarer harness from coming up short while still bounding the probe.
const HARNESS_SCAN_FACTOR = 8;
const MIN_PROBE_BATCH = 5;

// Load runs newest-first until `limit` usable pipeline runs are in hand, the
// scan cap is reached, or the candidates run out. Usability is only known
// AFTER a run is loaded (the adhoc flag lives in meta.json; a broken run.json
// surfaces as overview:null) — slicing exactly `limit` ids up front would let
// every unusable run silently shrink the window by one slot (loaded →
// dropped, slot wasted). The first batch is sized to the full deficit, so the
// common all-usable case stays a single fetch round.
// Preconditions: `ids` must be newest-first (the final slice keeps the head,
// i.e. the newest runs), and `load` must resolve rather than reject —
// mapWithConcurrency propagates a rejection, which would tank the whole page.
// loadPerRunForId honors this by downgrading failures to overview:null.
// Exported for unit testing.
export async function collectPipelineRuns(
    ids: string[],
    limit: number,
    load: (id: string) => Promise<PerRun>,
    // Optional extra predicate (AND-ed with usability) — e.g. a harness filter.
    // When set, the scan reaches further back since matches are sparser.
    isMatch?: (r: PerRun) => boolean,
    // Probe every candidate instead of stopping at a multiple of `limit`. A
    // capped scan can't tell "no older match exists" from "stopped looking", so
    // the paged run table — whose "Show more" link comes from finding one extra
    // row — has to probe fully or it silently hides older matching runs.
    probeAll = false,
): Promise<PerRun[]> {
    const scanFactor = isMatch ? HARNESS_SCAN_FACTOR : RECENT_SCAN_FACTOR;
    const maxScan = probeAll
        ? ids.length
        : Math.min(ids.length, limit * scanFactor);
    const usable = isMatch
        ? (r: PerRun) => isUsablePipelineRun(r) && isMatch(r)
        : isUsablePipelineRun;
    const out: PerRun[] = [];
    let cursor = 0;
    while (out.length < limit && cursor < maxScan) {
        // First round = the full deficit; only refill rounds get the floor.
        const batchSize =
            cursor === 0
                ? limit
                : Math.max(limit - out.length, MIN_PROBE_BATCH);
        const batch = ids.slice(cursor, Math.min(cursor + batchSize, maxScan));
        cursor += batch.length;
        const loaded = await mapWithConcurrency(batch, FETCH_CONCURRENCY, load);
        out.push(...loaded.filter(usable));
    }
    if (out.length < limit && cursor >= maxScan && cursor < ids.length) {
        console.warn(
            `[evalboard] collectPipelineRuns: stopped after probing ${cursor} ` +
                `candidates with ${out.length}/${limit} usable runs`,
        );
    }
    // The batch floor can overshoot the deficit on the final round; keep the
    // newest `limit` (out is filled newest-first).
    return out.slice(0, limit);
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
    // When set, scope the chart + rails to one harness. The nightly rotates
    // harnesses as separate runs, so an unscoped chart interleaves incomparable
    // pass rates into one zigzag line. null = all harnesses (legacy behavior).
    harness: string | null = null,
): Promise<OverviewData> {
    // Ad-hoc runs never feed the daily chart or the tag rails — they're not
    // pipeline cadence. (Non-date-named ones are already pruned upstream by
    // listRunIdsInWindow; this also drops date-named runs flagged adhoc.)
    const perRun = (await loadWindowData(window)).filter(
        (r) =>
            !r.adhoc &&
            (harness == null ||
                normalizeHarness(r.overview?.harness) === harness),
    );
    const needle = q?.trim().toLowerCase() || null;

    // ---- Per-run chart points + the window rollup ----
    // One chart point per run plotted at its own timestamp, no daily averaging,
    // and one rollup row per run for the summary tiles. Both come out of the
    // same scopeRunTasks call in the same loop, so the tiles can never describe
    // a different set of runs than the charts plot.
    const runPoints: RunPoint[] = [];
    const rows: RunListingRow[] = [];
    const seenHarnesses = new Set<string>();

    for (const r of perRun) {
        const { id, overview } = r;
        if (!overview || overview.tasks.length === 0) continue;
        const date = parseRunIdDate(id);
        if (!date) continue;
        const scoped = scopeRunTasks(r, tag, needle);
        if (!scoped || scoped.tasks.length === 0) continue;

        const row = rowFromScoped(id, scoped, overview.harness);
        rows.push(row);

        const runHarness = normalizeHarness(overview.harness);
        seenHarnesses.add(runHarness);
        runPoints.push({
            runId: id,
            timestamp: date.getTime(),
            harness: runHarness,
            successRate: (row.tasksSucceeded / row.tasksRun) * 100,
            turnBudgetRate: turnBudgetRateForTasks(scoped.tasks),
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
        harnesses: orderHarnesses(seenHarnesses),
        windowStart,
        windowEnd,
        totals: summarizeListing(rows),
        runCount: rows.length,
        skills,
        taskTags,
        reviewTags,
        activeTag: tag,
    };
}

export interface TagTaskRow {
    taskId: string;
    skill: string | null;
    // How many runs in the window carried this task under the tag.
    appearances: number;
    passRate: number; // 0-100 across those appearances
    latestStatus: string | null;
    latestScore: number | null;
    latestRunId: string;
}

// Per-task breakdown for a single tag, windowed like getOverview but grouped
// by task instead of by run. One row per distinct task_id carrying the tag
// anywhere in the window; "latest" fields come from the newest run the task
// appeared in (runs are walked newest-first, so the first occurrence wins).
export async function getTagTaskBreakdown(
    window: Window,
    tag: string,
    harness: string | null = null,
): Promise<TagTaskRow[]> {
    const perRun = (await loadWindowData(window)).filter(
        (r) =>
            !r.adhoc &&
            (harness == null ||
                normalizeHarness(r.overview?.harness) === harness),
    );
    const sorted = [...perRun].sort((a, b) => b.id.localeCompare(a.id));

    interface Acc {
        skill: string | null;
        statuses: (string | null)[];
        latestRunId: string;
        latestStatus: string | null;
        latestScore: number | null;
    }
    const byTask = new Map<string, Acc>();

    for (const { id, overview, reviewTagsByTask } of sorted) {
        if (!overview) continue;
        for (const t of overview.tasks) {
            if (!taskMatchesTag(t, reviewTagsByTask, tag)) continue;
            let entry = byTask.get(t.taskId);
            if (!entry) {
                entry = {
                    skill: t.skill,
                    statuses: [],
                    latestRunId: id,
                    latestStatus: t.status,
                    latestScore: t.weightedScore,
                };
                byTask.set(t.taskId, entry);
            }
            entry.statuses.push(t.status);
        }
    }

    const rows: TagTaskRow[] = [];
    for (const [taskId, e] of byTask) {
        const appearances = e.statuses.length;
        const passed = e.statuses.filter((s) => s === "SUCCESS").length;
        rows.push({
            taskId,
            skill: e.skill,
            appearances,
            passRate: appearances ? (passed / appearances) * 100 : 0,
            latestStatus: e.latestStatus,
            latestScore: e.latestScore,
            latestRunId: e.latestRunId,
        });
    }
    return rows.sort((a, b) => a.taskId.localeCompare(b.taskId));
}

// The slice of a run that the active tag/q filter selects: which tasks count,
// and the cost/duration summed over exactly those. null means the run has
// nothing matching and drops out entirely.
//
// This is the ONE definition of "does this run count, and over which tasks",
// shared by the chart points, the summary tiles, and the run table. Each used to
// carry its own copy, and a disagreement between them would have been invisible
// on the page.
export interface ScopedRun {
    tasks: RunOverviewTask[];
    totalCostUsd: number | null;
    taskDurationSeconds: number | null;
}

export function scopeRunTasks(
    { id, overview, reviewTagsByTask }: PerRun,
    tag: string | null,
    needle: string | null,
): ScopedRun | null {
    if (!overview) return null;
    const wholeRun: ScopedRun = {
        tasks: overview.tasks,
        totalCostUsd: overview.totalCostUsd,
        taskDurationSeconds: overview.taskDurationSeconds,
    };
    if (tag == null && needle == null) return wholeRun;

    const matching = overview.tasks.filter((t) => {
        const passesTag =
            tag == null || taskMatchesTag(t, reviewTagsByTask, tag);
        const passesQ =
            needle == null || taskMatchesQuery(t, reviewTagsByTask, needle);
        return passesTag && passesQ;
    });
    if (matching.length === 0) {
        // A `q` that matches only the run ID is the date-fragment "pin a run"
        // case: keep the whole-run slice so the row shows real totals rather
        // than 0/—/—. Anything else has nothing to show.
        const idMatchesQ = needle != null && id.toLowerCase().includes(needle);
        return idMatchesQ ? wholeRun : null;
    }

    // Cost sums any matching task that recorded one; duration is only meaningful
    // when every matching task has one (otherwise the partial sum would
    // understate the slice — mirrors readRunOverview's whole-run rule).
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
    return {
        tasks: matching,
        totalCostUsd: costHasAny ? costSum : null,
        taskDurationSeconds: durAllPresent ? durSum : null,
    };
}

function rowFromScoped(
    id: string,
    scoped: ScopedRun,
    harness: string | null | undefined,
): RunListingRow {
    return {
        id,
        tasksSucceeded: scoped.tasks.filter((t) => t.status === "SUCCESS")
            .length,
        tasksRun: scoped.tasks.length,
        totalCostUsd: scoped.totalCostUsd,
        taskDurationSeconds: scoped.taskDurationSeconds,
        harness: harness ?? null,
    };
}

// One loaded run as a table row, or null when the filter excludes it. Exported
// for unit testing.
export function projectRunRow(
    run: PerRun,
    tag: string | null,
    needle: string | null,
): RunListingRow | null {
    const scoped = scopeRunTasks(run, tag, needle);
    return scoped ? rowFromScoped(run.id, scoped, run.overview?.harness) : null;
}

// The run table. Unlike getOverview's window rollup, this is NOT date-bounded:
// it pages back through the whole store, a screenful at a time, so history older
// than the charts' window is still reachable without a time-window control.
//
// Rows are loaded lazily because a run.json is multi-MB — reading all of history
// to count matches would cost hundreds of MB of parsing per render. So the depth
// of the read is the depth of the page: `hasMore` comes from over-fetching a
// single row rather than from a total, and the only free count (every pipeline
// candidate in the store) is reported separately as `totalCandidates`.
export async function getRunListing(
    tag: string | null,
    q: string | null,
    limit: number,
    // When set, scope the listing to one harness — applied at the same seam as
    // the chart's scope in getOverview, so the tiles, the charts, and the table
    // always describe the same set of runs. null = all harnesses.
    harness: string | null = null,
): Promise<RunListing> {
    // Ad-hoc ids aren't date-shaped, so this drops them (they have their own
    // section) without loading anything. Newest-first: ids are timestamps.
    const ids = (await listRunIds()).filter((id) => parseRunIdDate(id) != null);
    const needle = q?.trim().toLowerCase() || null;
    const hasFilter = tag != null || needle != null || harness != null;
    const isMatch = hasFilter
        ? (r: PerRun) =>
              (harness == null ||
                  normalizeHarness(r.overview?.harness) === harness) &&
              projectRunRow(r, tag, needle) != null
        : undefined;

    // Over-fetch by one: if the extra row materializes, another page exists.
    // probeAll, because a scan that gave up at a cap would report `hasMore:
    // false` and strand every older matching run behind a link that vanished.
    const loaded = await collectPipelineRuns(
        ids,
        limit + 1,
        cachedLoadPerRun,
        isMatch,
        true,
    );
    const rows = loaded
        .map((r) => projectRunRow(r, tag, needle))
        .filter((row): row is RunListingRow => row != null);

    return {
        rows: rows.slice(0, limit),
        totalCandidates: ids.length,
        hasMore: rows.length > limit,
    };
}

export interface AdhocRunRow extends RunListingRow {
    // From meta.json; null when the run predates the feature (then the UI
    // falls back to the run id).
    title: string | null;
    // run.json start_time (ISO). The ad-hoc id carries no date, so this is the
    // row's only date — used to sort the section and render the Date column.
    // null on a run whose run.json omitted start_time.
    startedAt: string | null;
}

export interface AdhocListing {
    // Sorted newest-first and capped to the caller's limit.
    rows: AdhocRunRow[];
    // Pre-limit count — drives the section's "{shown} of {total}" label and
    // whether a "Show all" affordance is offered.
    total: number;
}

// Project loaded ad-hoc runs into rows, newest-first by run start_time, and
// capped to `limit` (null = unlimited). Ad-hoc ids aren't date-shaped, so —
// unlike the pipeline listing, which sorts/windows by id — the date lives in
// run.json (startedAt). ISO timestamps sort chronologically as strings, so a
// plain descending string compare orders them; runs missing a start_time sort
// last (empty key), then by id descending for a deterministic tie-break. Runs
// without a readable overview (aborted uploads that left only default/) are
// dropped. `total` is the pre-limit count, so the UI can offer "Show all".
// Exported for testing.
export function buildAdhocRows(
    perRun: PerRun[],
    limit: number | null,
): AdhocListing {
    const rows: AdhocRunRow[] = [];
    for (const { id, overview, title } of perRun) {
        if (!overview) continue;
        rows.push({
            id,
            title: title ?? null,
            startedAt: overview.startedAt ?? null,
            tasksSucceeded: overview.tasks.filter((t) => t.status === "SUCCESS")
                .length,
            tasksRun: overview.tasks.length,
            totalCostUsd: overview.totalCostUsd,
            taskDurationSeconds: overview.taskDurationSeconds,
            // Harness is a main-table-only (internal) column; ad-hoc rows omit it.
        });
    }
    rows.sort(
        (a, b) =>
            (b.startedAt ?? "").localeCompare(a.startedAt ?? "") ||
            b.id.localeCompare(a.id),
    );
    return {
        rows: limit == null ? rows : rows.slice(0, limit),
        total: rows.length,
    };
}

// The Ad-hoc runs section (front page, below the daily listing). "Ad-hoc" here
// means "not a daily-pipeline run" — i.e. the id isn't date-shaped, which is
// exactly the set listRunIdsInWindow excludes from the chart and main table.
// Every ad-hoc candidate is loaded before sorting (the date lives in run.json,
// not the id, so we can't window by id and still show the most recent): the
// ad-hoc set is small by construction (manual uploads only) and per-run reads
// are memoized for 5 min, so a warm front page pays no extra IO.
export async function getAdhocRunListing(limit: number | null): Promise<AdhocListing> {
    const ids = (await listRunIds()).filter((id) => parseRunIdDate(id) == null);
    const perRun = await mapWithConcurrency(ids, FETCH_CONCURRENCY, cachedLoadPerRun);
    return buildAdhocRows(perRun, limit);
}
