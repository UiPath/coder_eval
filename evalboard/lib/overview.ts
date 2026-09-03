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
import { DEFAULT_SOURCE, type Source } from "./sources";
import { withinExpectedTime } from "./timing";
import { withinTurnBudget } from "./turns";
import { humanizeTaskId } from "./format";
import { mapWithConcurrency } from "./concurrency";
import { DEFAULT_HARNESS, normalizeHarness, orderHarnesses } from "./harness";
import { isPassStatus } from "./status";
import { taskCarriesRepoTag } from "./tags";
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
    //
    // Superseded by the two wall-clock metrics below and kept only while both
    // signals run side by side; the chart behind the "Turns" tab is the only
    // reader left, so retiring the turn budget is that tab plus this field.
    turnBudgetRate: number | null;
    // % of scored passing tasks that came in within 2× their expected wall clock.
    // Only tasks that passed AND carry a derived expected_seconds are eligible,
    // so the rate is stable under tag/q filtering. null when no task in scope is
    // scored (the chart shows a gap rather than a failure-driven 0%).
    withinExpectedTimeRate: number | null;
    // Seconds of every task that ran over the number that passed, for this run's
    // scoped task set. Failures stay in the numerator on purpose. null when
    // nothing in scope passed or no duration was recorded.
    timePerPassedTask: number | null;
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
// pass/fail. See turnRatio's comment. The wall-clock rate below does not make
// that trade: it scores passes only.
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

// The % of scored passing tasks that came in within 2× their expected wall
// clock; null when no task in scope is scored.
//
// Passing tasks only, unlike the turn budget above: a task that crashed
// in 10 seconds did not blow a time budget, and counting it would make a
// pass-to-timeout regression read as a gain. Failure is the pass rate's job.
//
// Unscored tasks (a harness that has never passed the task, or a run predating
// the stamp) are excluded, so the rate does not shift when filtering changes
// which co-scoped tasks happen to be scored. Exported for unit testing.
export function withinExpectedTimeRateForTasks(
    tasks: RunOverviewTask[],
): number | null {
    let eligible = 0;
    let within = 0;
    for (const t of tasks) {
        if (t.status !== "SUCCESS" || t.matureSkipped) continue;
        const verdict = withinExpectedTime(t.durationSeconds, t.expectedSeconds);
        if (verdict === null) continue; // unscored, or no duration → can't judge
        eligible += 1;
        if (verdict) within += 1;
    }
    // Nothing scored in scope → nothing to report (not a failure-driven 0%).
    return eligible > 0 ? (within / eligible) * 100 : null;
}

// Seconds of every task that ran, over the number that passed. Mirrors the
// runner's headline (timing.py::time_per_passed_task) so a filtered front-page
// view and the block stamped into run.json compute the same thing on the same
// rows. The Slack rollup does not report this yet — the metric is being watched
// on the dashboard first.
//
// Mature-skipped rows leave BOTH sides. They are carried-forward passes with no
// duration, so counting them only in the denominator divides real seconds by a
// task count that never ran: on a codex nightly that reported 3m12s, including
// them read 1m17s.
export function timePerPassedTaskForTasks(
    tasks: RunOverviewTask[],
): number | null {
    const executed = tasks.filter((t) => !t.matureSkipped);
    const passed = executed.filter((t) => t.status === "SUCCESS").length;
    if (!passed) return null;
    const total = executed.reduce((a, t) => a + (t.durationSeconds ?? 0), 0);
    return total > 0 ? total / passed : null;
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

async function loadPerRunForId(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<PerRun> {
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
            readRunOverview(id, source),
            readRunReviewIndex(id, source),
            readRunMeta(id, source),
        ]);
    } catch (err) {
        console.error(
            `[evalboard] loadPerRunForId(${source.id}/${id}) failed:`,
            err,
        );
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

// First `YYYY-MM-DD` (optionally `_HH-MM-SS`) anywhere in a run id. Ad-hoc ids
// aren't date-SHAPED (parseRunIdDate anchors, so it rejects them) but almost all
// still carry their date: `adhoc-2026-09-02_21-59-13`, `skills-2026-05-28`.
const ADHOC_DATE_RE = /(\d{4})-(\d{2})-(\d{2})(?:_(\d{2})-(\d{2})-(\d{2}))?/;

// Id-only start date for an ad-hoc run; null when the id carries no date. A
// date-only id resolves to the start of its day, which can only lose a same-day
// tie-break.
export function adhocRunDate(id: string): Date | null {
    const m = ADHOC_DATE_RE.exec(id);
    if (!m) return null;
    const [, y, mo, d, h, mi, s] = m;
    const t = Date.UTC(
        Number(y),
        Number(mo) - 1,
        Number(d),
        Number(h ?? 0),
        Number(mi ?? 0),
        Number(s ?? 0),
    );
    return Number.isFinite(t) ? new Date(t) : null;
}

// Cache at per-run granularity, NOT per-window. A whole-window PerRun[] for
// the 30d window exceeds unstable_cache's hard 2MB ceiling (≈400 tasks × ~20
// runs), and on overflow Next.js drops the write AND hands back a truncated
// payload — silently shearing off the newest runs (the 30d front page lost
// "today"). One run's projection is well under 2MB, so keying the cache on the
// run id keeps every entry cacheable and lets entries be reused across all
// windows + the trends page. The cross-run aggregation downstream is cheap
// in-memory work, so leaving it uncached costs nothing.
//
// One cached loader per (source, run), with `source.id` in the key parts. Run
// ids are only unique WITHIN a container — both suites name runs
// `YYYY-MM-DD_HH-MM-SS`, so a source-blind key would let a Scribe run and a
// skills run with the same id serve each other's projection.
//
// Per-run rather than per-source because `unstable_cache` fixes revalidate and
// tags at construction: one shared loader can hold neither a per-run TTL nor a
// per-run tag for the refresh button to evict.
type PerRunLoader = (id: string) => Promise<PerRun>;

// Keyed `<source>:<run>`; grows with history, not with traffic.
const perRunLoaders = new Map<string, () => Promise<PerRun>>();

// A run.json is written once, at the end of the run, so a finished run is
// immutable. Its sidecars (meta.json, reviews, analysis) are not, which is what
// `runCacheTag` is for.
const SETTLED_AFTER_MS = 24 * 60 * 60 * 1000;
// Recent enough that today's nightly may still be landing.
const FRESH_REVALIDATE_SECONDS = 300;
// Older than that: the read this avoids is a multi-MB run.json off Azure Files.
const SETTLED_REVALIDATE_SECONDS = 24 * 60 * 60;

// Cache tag for one run's projection, so POST /api/refresh can evict it;
// without it the button would no-op for anything past SETTLED_AFTER_MS.
export function runCacheTag(sourceId: string, runId: string): string {
    return `evalboard-run:${sourceId}:${runId}`;
}

// Settled runs cache for a day, everything else for 5 minutes. An id carrying no
// date at all is treated as fresh: those are hand-uploaded and re-uploaded far
// more often than pipeline runs. Evaluated once per run per process, so a run
// that settles while the process is up keeps the short TTL until restart.
function perRunRevalidate(id: string): number {
    const started = parseRunIdDate(id) ?? adhocRunDate(id);
    if (started == null) return FRESH_REVALIDATE_SECONDS;
    return Date.now() - started.getTime() > SETTLED_AFTER_MS
        ? SETTLED_REVALIDATE_SECONDS
        : FRESH_REVALIDATE_SECONDS;
}

function cachedLoadPerRunFor(source: Source): PerRunLoader {
    return (id: string) => {
        const cacheKey = `${source.id}:${id}`;
        let loader = perRunLoaders.get(cacheKey);
        if (!loader) {
            loader = unstable_cache(
                () => loadPerRunForId(id, source),
                ["evalboard-per-run", source.id, id],
                {
                    revalidate: perRunRevalidate(id),
                    tags: [runCacheTag(source.id, id)],
                },
            );
            perRunLoaders.set(cacheKey, loader);
        }
        return loader();
    };
}

async function loadWindowDataInner(
    window: Window,
    source: Source = DEFAULT_SOURCE,
): Promise<PerRun[]> {
    const ids = await listRunIdsInWindow(window, source);
    return mapWithConcurrency(
        ids,
        FETCH_CONCURRENCY,
        cachedLoadPerRunFor(source),
    );
}

// Fetch the N most recent runs in PerRun shape. Recency-based (fixed count)
// rather than date-bounded — used by the trends page. When `harness` is set,
// only runs of that harness count toward N (the nightly now rotates
// claude-code / codex / antigravity, and a trend is only meaningful within one
// harness — mixing them blends incomparable pass rates and cost profiles).
export function loadRecentRuns(
    limit: number,
    harness?: string,
    source: Source = DEFAULT_SOURCE,
): Promise<PerRun[]> {
    return loadRecentRunsInner(limit, harness, source);
}

async function loadRecentRunsInner(
    limit: number,
    harness?: string,
    source: Source = DEFAULT_SOURCE,
): Promise<PerRun[]> {
    // Trends is the daily-cadence view: only pipeline runs belong here. Prune
    // to date-shaped ids BEFORE slicing (cheap, no IO) so ad-hoc runs — whose
    // ids sort lexically above every `2026-…` daily id and would otherwise
    // crowd out the real "recent N" — never occupy a slot.
    const ids = (await listRunIds(source)).filter(
        (id) => parseRunIdDate(id) != null,
    );
    const matchesHarness = harness
        ? (r: PerRun) => normalizeHarness(r.overview?.harness) === harness
        : undefined;
    return collectPipelineRuns(
        ids,
        limit,
        cachedLoadPerRunFor(source),
        matchesHarness,
    );
}

// How many recent runs to scan when discovering which harnesses are active.
// All harnesses that run at least weekly appear within a window this size, so
// the switcher lists them without a hardcoded set (a new harness like
// "delegate" shows up on its own).
const HARNESS_DISCOVERY_COUNT = 12;

async function listRecentHarnessesInner(
    source: Source = DEFAULT_SOURCE,
): Promise<string[]> {
    const perRun = await loadRecentRuns(
        HARNESS_DISCOVERY_COUNT,
        undefined,
        source,
    );
    const seen = new Set<string>();
    for (const r of perRun) {
        if (r.overview) seen.add(normalizeHarness(r.overview.harness));
    }
    const ordered = orderHarnesses(seen);
    // Never hand back an empty list — the default must always be selectable.
    return ordered.length > 0 ? ordered : [DEFAULT_HARNESS];
}

// One cached discovery per source, with `source.id` in the key parts — each
// source has its own set of active harnesses, and the ids they're discovered
// from collide across containers.
const recentHarnessLoaders = new Map<string, () => Promise<string[]>>();

// The harnesses present in recent runs, ordered for the switcher. Cached (and
// shares the per-run cache with the aggregates), revalidated every 5 min.
export function listRecentHarnesses(
    source: Source = DEFAULT_SOURCE,
): Promise<string[]> {
    let loader = recentHarnessLoaders.get(source.id);
    if (!loader) {
        loader = unstable_cache(
            () => listRecentHarnessesInner(source),
            ["recent-harnesses-v1", source.id],
            { revalidate: 300 },
        );
        recentHarnessLoaders.set(source.id, loader);
    }
    return loader();
}

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
// memoized per run inside cachedLoadPerRunFor; gathering them for a window is
// cheap, so this stays uncached (and avoids the 2MB whole-window cache cap).
function loadWindowData(
    window: Window,
    source: Source = DEFAULT_SOURCE,
): Promise<PerRun[]> {
    return loadWindowDataInner(window, source);
}

// Repo-provenance half of taskMatchesTag. Defined in the dependency-free
// lib/tags.ts (this module is server-only — it imports next/cache — so a
// "use client" component could not adopt a copy living here) and re-exported
// for the existing callers.
export { taskCarriesRepoTag };

export function taskMatchesTag(
    task: RunOverviewTask,
    reviewTagsByTask: Record<string, string[]>,
    tag: string,
): boolean {
    if (taskCarriesRepoTag(task, tag)) return true;
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
    source: Source = DEFAULT_SOURCE,
): Promise<OverviewData> {
    // Ad-hoc runs never feed the daily chart or the tag rails — they're not
    // pipeline cadence. (Non-date-named ones are already pruned upstream by
    // listRunIdsInWindow; this also drops date-named runs flagged adhoc.)
    const perRun = (await loadWindowData(window, source)).filter(
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
            withinExpectedTimeRate: withinExpectedTimeRateForTasks(
                scoped.tasks,
            ),
            timePerPassedTask: timePerPassedTaskForTasks(scoped.tasks),
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
    // Tagged task ROWS in the window, not distinct runs: a replicated task
    // contributes one per replicate. Unchanged from the previous behaviour
    // (the column header stays "Appearances") — the de-tag check below is the
    // only place that collapses to one sample per run. Includes rows the
    // nightly skipped as mature and carried forward.
    appearances: number;
    // Of `appearances`, how many were mature carry-forwards (not executed).
    matureSkips: number;
    // `appearances - matureSkips`: the denominator behind passRate, carried on
    // the row rather than re-derived by the renderer so the percentage and the
    // caption that names its sample size can never describe different rules.
    executed: number;
    // 0-100 over EXECUTED appearances only (appearances - matureSkips).
    // null when nothing in the window actually ran, so the UI shows "—"
    // rather than a measured-looking 0% or 100%.
    passRate: number | null;
    latestStatus: string | null;
    latestScore: number | null;
    latestRunId: string;
    // True when the newest tagged ROW — the same row latestStatus and
    // latestScore come from — was a mature carry-forward, so those two are
    // inherited, not measured.
    latestMatureSkipped: boolean;
}

// Per-task breakdown for a single tag, windowed like getOverview but grouped by
// task instead of by run. Pure over the PerRun[] it is handed (the caller does
// the fetching and the adhoc/harness filtering), so it unit-tests without
// touching the blob store — mirrors lib/trends.ts::aggregate.
//
// DE-TAGGING. A run.json task row carries `tags` as a historical stamp written
// at execution time; the board never consults the skills repo. So a task
// de-tagged upstream keeps rendering for as long as a run that predates the
// removal stays in the window. The rule here: a task is dropped when it appears
// in a NEWER run in the window whose rows for it do not carry the tag — that is
// proof the tag was removed, not a heuristic. A task that merely stopped
// appearing (retired, renamed, `skip: true`) is unknowable and therefore KEPT,
// with `latestRunId` doubling as "last seen" so its age is visible.
//
// The signal comes from taskCarriesRepoTag, NOT taskMatchesTag: review tags
// (review_index.json) are post-hoc annotations from a separate namespace, so an
// as-yet-unreviewed newest run — the normal case — would otherwise read as a
// de-tag. The same predicate is used to accumulate, so the narrowing is
// symmetric: a task pulled onto this page only by a review tag does not appear.
//
// The "did any row carry the tag this run" collapse is required because a
// replicated task has several rows per run, and one untagged replicate must not
// read as a de-tag.
//
// MATURITY — a DELIBERATE, page-local divergence. lib/trends.ts::aggregate (and
// app/runs/[id]/run-view.tsx) count a mature carry-forward as a pass and exclude
// it only from the cost/duration averages. Here it is excluded from BOTH the
// numerator and the denominator of `passRate`, because /path-to-ga is a
// GA-readiness page and must report MEASURED passes. That difference is
// intentional — do not "harmonise" this with trends.ts.
//
// CROSS-REPO CONTRACT: `matureSkipped` is stamped into run.json by the external
// nightly eval_runner, not by anything in src/coder_eval. If the producer renames
// or drops the field every carry-forward silently reads as an executed pass again
// — the rate inflates, the "(N mature)" annotations and Mature pills vanish, and
// nothing errors. It is the one input here this repo cannot type-check.
export function buildTagTaskRows(perRun: PerRun[], tag: string): TagTaskRow[] {
    // Run ids are date-shaped, so a lexical sort is chronological — the same
    // assumption lib/trends.ts::aggregate and the previous implementation
    // already make.
    const sorted = [...perRun].sort((a, b) => b.id.localeCompare(a.id));

    interface Acc {
        skill: string | null;
        appearances: number;
        matureSkips: number;
        executedPasses: number;
        latestRunId: string;
        latestStatus: string | null;
        latestScore: number | null;
        latestMatureSkipped: boolean;
    }
    const byTask = new Map<string, Acc>();
    // taskId -> did its NEWEST appearance in the window carry the tag. First
    // write wins because the walk is newest-first, so a task that only gained
    // the tag recently reads as tagged (and one that lost it reads as untagged)
    // regardless of what the older runs say.
    const newestTagged = new Map<string, boolean>();

    for (const { id, overview } of sorted) {
        // A run whose run.json failed to load (loadPerRunForId downgrades to a
        // null overview) must contribute neither an appearance nor a de-tag
        // signal — otherwise a transient blob failure on the newest run would
        // drop every row.
        if (!overview) continue;

        // Two passes over the run's rows, not one: `taggedInRun` must be
        // complete before any verdict is recorded, because a replicated task
        // has several rows per run and one untagged replicate must not read as
        // a de-tag. The `newestTagged.has` guard below is what collapses those
        // replicate rows to a single first-write-wins verdict.
        const taggedInRun = new Set<string>();
        for (const t of overview.tasks) {
            if (taskCarriesRepoTag(t, tag)) taggedInRun.add(t.taskId);
        }
        for (const t of overview.tasks) {
            if (!newestTagged.has(t.taskId)) {
                newestTagged.set(t.taskId, taggedInRun.has(t.taskId));
            }
        }

        for (const t of overview.tasks) {
            if (!taskCarriesRepoTag(t, tag)) continue;
            let entry = byTask.get(t.taskId);
            if (!entry) {
                // All three latest* fields come off ONE row — the first tagged
                // row of the newest-first walk — so the Mature pill and the
                // dashed-out score always describe the same sample.
                entry = {
                    skill: t.skill,
                    appearances: 0,
                    matureSkips: 0,
                    executedPasses: 0,
                    latestRunId: id,
                    latestStatus: t.status,
                    latestScore: t.weightedScore,
                    latestMatureSkipped: t.matureSkipped ?? false,
                };
                byTask.set(t.taskId, entry);
            }
            entry.appearances += 1;
            if (t.matureSkipped) {
                entry.matureSkips += 1;
            } else if (isPassStatus(t.status)) {
                // lib/status.ts, not a raw "SUCCESS" literal: `status` is an
                // untyped string, and this page's pass rate must move with
                // every other surface if the passing set ever widens.
                entry.executedPasses += 1;
            }
        }
    }

    const rows: TagTaskRow[] = [];
    for (const [taskId, e] of byTask) {
        // Provably de-tagged: the task is still running, and its newest run does
        // not carry the tag. (A task in byTask always has a newestTagged entry —
        // both are written from the same non-null-overview iteration — so the
        // `?? true` only satisfies Map.get's `| undefined`; it is not a real
        // "unknown ⇒ keep" case.)
        if (!(newestTagged.get(taskId) ?? true)) continue;
        const executed = e.appearances - e.matureSkips;
        rows.push({
            taskId,
            skill: e.skill,
            appearances: e.appearances,
            matureSkips: e.matureSkips,
            executed,
            passRate: executed > 0 ? (e.executedPasses / executed) * 100 : null,
            latestStatus: e.latestStatus,
            latestScore: e.latestScore,
            latestRunId: e.latestRunId,
            latestMatureSkipped: e.latestMatureSkipped,
        });
    }
    return rows.sort((a, b) => a.taskId.localeCompare(b.taskId));
}

// IO wrapper around buildTagTaskRows: fetch the window, drop ad-hoc runs and
// (optionally) scope to one harness, then aggregate. Harness scoping happens
// HERE, before the pure function sees the runs, so a newer run on a different
// harness cannot de-tag a row in a harness-scoped view.
export async function getTagTaskBreakdown(
    window: Window,
    tag: string,
    harness: string | null = null,
    source: Source = DEFAULT_SOURCE,
): Promise<TagTaskRow[]> {
    const perRun = (await loadWindowData(window, source)).filter(
        (r) =>
            !r.adhoc &&
            (harness == null ||
                normalizeHarness(r.overview?.harness) === harness),
    );
    return buildTagTaskRows(perRun, tag);
}

// Mean of the per-run success rates, over the runs that HAVE one. A run whose
// successRate is null has no measurable outcome (no tasks, or a run.json that
// failed to load) — folding it in as 0 would drag the headline tile down and
// make "no data" indistinguishable from "everything failed". null when no run
// in scope reports a rate at all.
export function avgRunSuccessRate(
    runs: readonly { successRate: number | null }[],
): number | null {
    const rates = runs
        .map((r) => r.successRate)
        .filter((r): r is number => r != null);
    if (rates.length === 0) return null;
    return rates.reduce((sum, r) => sum + r, 0) / rates.length;
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
    source: Source = DEFAULT_SOURCE,
): Promise<RunListing> {
    // Ad-hoc ids aren't date-shaped, so this drops them (they have their own
    // section) without loading anything. Newest-first: ids are timestamps.
    const ids = (await listRunIds(source)).filter(
        (id) => parseRunIdDate(id) != null,
    );
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
        cachedLoadPerRunFor(source),
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

// Extra candidates loaded beyond `limit`, covering ids with no readable overview
// (aborted uploads, the `deploys/` prefix) that then drop out of the rows.
const ADHOC_LOAD_SLACK = 10;

// The Ad-hoc runs section (front page, below the daily listing). "Ad-hoc" here
// means "not a daily-pipeline run" — i.e. the id isn't date-shaped, which is
// exactly the set listRunIdsInWindow excludes from the chart and main table.
//
// Only the newest `limit + ADHOC_LOAD_SLACK` candidates are loaded, ordered by
// the date in the id; loading all 165 to show ten rows cost ~294 MB per cold
// render. run.json's `start_time` stays the authoritative sort, so the id only
// decides what to READ. Ids with no date are always loaded, so they can never be
// ordered out by a key they don't have.
export async function getAdhocRunListing(
    limit: number | null,
    source: Source = DEFAULT_SOURCE,
): Promise<AdhocListing> {
    const ids = (await listRunIds(source)).filter(
        (id) => parseRunIdDate(id) == null,
    );
    const dated: { id: string; at: number }[] = [];
    const undated: string[] = [];
    for (const id of ids) {
        const at = adhocRunDate(id);
        if (at == null) undated.push(id);
        else dated.push({ id, at: at.getTime() });
    }
    dated.sort((a, b) => b.at - a.at);
    // null limit = "load everything": allowed by the signature, never used.
    const budget = limit == null ? dated.length : limit + ADHOC_LOAD_SLACK;
    const loadedAll = budget >= dated.length;
    const toLoad = [...undated, ...dated.slice(0, budget).map((d) => d.id)];
    const perRun = await mapWithConcurrency(
        toLoad,
        FETCH_CONCURRENCY,
        cachedLoadPerRunFor(source),
    );
    const listing = buildAdhocRows(perRun, limit);
    // `total` drives "Show more", so a truncated load must report the candidate
    // count or the section caps itself at the first page.
    return loadedAll ? listing : { ...listing, total: ids.length };
}
