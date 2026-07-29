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
import { DEFAULT_HARNESS, KNOWN_HARNESSES, normalizeHarness } from "./harness";
import type { Window } from "./reviews-types";

export interface RunPoint {
    runId: string;
    timestamp: number; // ms since epoch (UTC); used as the chart x-coordinate
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
    totalInWindow: number;
    matchedCount: number; // post-filter, pre-limit
    // Rollup across all matched runs (pre-limit) — powers the front-page
    // window summary; independent of the `rows` limit.
    totals: RunListingTotals;
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
    // Known harnesses first (stable display order), then any newcomers
    // (alphabetical) so the list is deterministic but self-extending.
    const known = KNOWN_HARNESSES.filter((h) => seen.has(h));
    const extras = [...seen]
        .filter((h) => !(KNOWN_HARNESSES as readonly string[]).includes(h))
        .sort();
    const ordered = [...known, ...extras];
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
): Promise<PerRun[]> {
    const scanFactor = isMatch ? HARNESS_SCAN_FACTOR : RECENT_SCAN_FACTOR;
    const maxScan = Math.min(ids.length, limit * scanFactor);
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
            turnBudgetRate: turnBudgetRateForTasks(matching),
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
            harness: overview.harness ?? null,
        });
    }

    const rows = limit == null ? matched : matched.slice(0, limit);
    return {
        rows,
        totalInWindow,
        matchedCount: matched.length,
        totals: summarizeListing(matched),
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
