// Membership needs only meta.json; per-arm numbers come from experiment.json for
// variant series and from the run overview otherwise, so a two-arm run never
// reads its multi-MB run.json except for the detail page's run-level metrics.

import { unstable_cache } from "next/cache";
import {
    cachedLoadPerRunFor,
    perRunRevalidate,
    runCacheTag,
    adhocRunDate,
    timePerPassedTaskForTasks,
} from "./overview";
import { parseRunIdDate } from "./reviews";
import {
    listRunIds,
    readRunExperiment,
    readRunMeta,
    type RunExperiment,
    type RunOverview,
} from "./runs";
import { createLimiter, mapWithConcurrency } from "./concurrency";
import { DEFAULT_SOURCE, type Source } from "./sources";
import { isGraded, isPassStatus, statusCategory } from "./status";
import {
    SINGLE_ARM,
    membersOf,
    plotted,
    resolveArms,
    seriesHealth,
    unlistedSeriesKeys,
    latestValues,
    armGap,
    type ArmValues,
    type SeriesArm,
    type SeriesDef,
    type SeriesHealth,
    type SeriesPoint,
    type SeriesRunRef,
} from "./series";

const DAY_MS = 86_400_000;
const LOOKBACK_DAYS = 120;
const REFS_REVALIDATE_SECONDS = 300;
const FRESH_RUN_MS = 2 * DAY_MS;
const META_REFRESH_FRESH_MS = 60_000;
const META_REFRESH_SETTLED_MS = 3_600_000;
const FETCH_CONCURRENCY = 16;
const STATS_CONCURRENCY = 8;
const MIN_STATS_BATCH = 3;
const SCAN_FACTOR = 3;

export const SPARK_RUNS = 8;
export const DETAIL_RUNS = 30;

async function readRunRefs(source: Source, nowMs: number): Promise<SeriesRunRef[]> {
    const cutoff = nowMs - LOOKBACK_DAYS * DAY_MS;
    const candidates = (await listRunIds(source)).flatMap((id) => {
        const at = adhocRunDate(id)?.getTime();
        return at != null && at >= cutoff ? [{ id, startMs: at }] : [];
    });
    return mapWithConcurrency(
        candidates,
        FETCH_CONCURRENCY,
        async ({ id, startMs }): Promise<SeriesRunRef> => {
            const meta = await readRunMeta(
                id,
                source,
                nowMs - startMs < FRESH_RUN_MS
                    ? META_REFRESH_FRESH_MS
                    : META_REFRESH_SETTLED_MS,
            );
            return {
                id,
                startMs,
                dateShaped: parseRunIdDate(id) != null,
                adhoc: meta?.adhoc ?? false,
                title: meta?.title ?? null,
                series: meta?.series ?? null,
                excluded: meta?.excluded ?? null,
            };
        },
    );
}

const refsLoaders = new Map<string, () => Promise<SeriesRunRef[]>>();

export function loadRunRefs(source: Source = DEFAULT_SOURCE): Promise<SeriesRunRef[]> {
    let loader = refsLoaders.get(source.id);
    if (!loader) {
        loader = unstable_cache(
            () => readRunRefs(source, Date.now()),
            ["evalboard-series-refs", source.id],
            { revalidate: REFS_REVALIDATE_SECONDS },
        );
        refsLoaders.set(source.id, loader);
    }
    return loader();
}

export async function loadSeriesMembers(def: SeriesDef): Promise<SeriesRunRef[]> {
    return membersOf(def, await loadRunRefs(def.source));
}

export async function loadUnlistedSeriesKeys(): Promise<string[]> {
    return unlistedSeriesKeys(await loadRunRefs(DEFAULT_SOURCE));
}

// ---------- per-run numbers ----------

export interface ArmStat {
    passRate: number | null;
    passed: number;
    graded: number;
    errors: number;
}

export interface RunStats {
    id: string;
    arms: Record<string, ArmStat>;
}

const experimentLoaders = new Map<string, () => Promise<RunExperiment | null>>();

function loadExperiment(id: string, source: Source): Promise<RunExperiment | null> {
    const key = `${source.id}:${id}`;
    let loader = experimentLoaders.get(key);
    if (!loader) {
        loader = unstable_cache(
            () => readRunExperiment(id, source),
            ["evalboard-experiment", source.id, id],
            {
                revalidate: perRunRevalidate(id),
                tags: [runCacheTag(source.id, id)],
            },
        );
        experimentLoaders.set(key, loader);
    }
    return loader();
}

export function statsFromExperiment(id: string, exp: RunExperiment): RunStats {
    const arms: Record<string, ArmStat> = {};
    for (const variantId of exp.variantIds) {
        const a = exp.arms[variantId];
        arms[variantId] = {
            passRate: a.passRate,
            passed: a.tasksSucceeded,
            graded: a.tasksGraded ?? a.tasksRun,
            errors: a.tasksError,
        };
    }
    return { id, arms };
}

export function statsFromOverview(
    id: string,
    overview: RunOverview | null,
): RunStats | null {
    if (!overview || overview.tasks.length === 0) return null;
    const passed = overview.tasks.filter((t) => isPassStatus(t.status)).length;
    const graded = overview.tasks.filter((t) => isGraded(t.status)).length;
    const errors = overview.tasks.filter(
        (t) => statusCategory(t.status) === "error",
    ).length;
    return {
        id,
        arms: {
            [SINGLE_ARM.id]: {
                passRate: graded ? (passed / graded) * 100 : null,
                passed,
                graded,
                errors,
            },
        },
    };
}

async function readRunStats(def: SeriesDef, id: string): Promise<RunStats | null> {
    if (def.arms.kind !== "single") {
        const exp = await loadExperiment(id, def.source);
        if (exp) return statsFromExperiment(id, exp);
        if (def.arms.kind === "variants") return null;
    }
    const run = await cachedLoadPerRunFor(def.source)(id);
    return statsFromOverview(id, run.overview);
}

export async function loadRunStats(
    def: SeriesDef,
    id: string,
): Promise<RunStats | null> {
    try {
        return await readRunStats(def, id);
    } catch (err) {
        console.error(`[evalboard] series stats ${def.source.id}/${id} failed:`, err);
        return null;
    }
}

export interface RunLevel {
    costPerTask: number | null;
    totalCostUsd: number | null;
    errorShare: number | null;
    timePerPassedTask: number | null;
}

export function runLevel(overview: RunOverview | null): RunLevel | null {
    if (!overview || overview.tasks.length === 0) return null;
    const graded = overview.tasks.filter((t) => isGraded(t.status)).length;
    const errors = overview.tasks.filter(
        (t) => statusCategory(t.status) === "error",
    ).length;
    const executed = overview.tasksExecuted ?? overview.tasks.length;
    return {
        costPerTask:
            overview.totalCostUsd != null && executed > 0
                ? overview.totalCostUsd / executed
                : null,
        totalCostUsd: overview.totalCostUsd,
        errorShare: graded ? (errors / graded) * 100 : null,
        timePerPassedTask: timePerPassedTaskForTasks(overview.tasks),
    };
}

// Every run-list row asks for its cost at once, and each cold load parses a
// multi-MB run.json.
const runLevelSlot = createLimiter(STATS_CONCURRENCY);

export async function loadRunLevel(
    def: SeriesDef,
    id: string,
): Promise<RunLevel | null> {
    try {
        const run = await runLevelSlot(() => cachedLoadPerRunFor(def.source)(id));
        return runLevel(run.overview);
    } catch (err) {
        console.error(`[evalboard] series run level ${def.source.id}/${id} failed:`, err);
        return null;
    }
}

export function pointFromStats(ref: SeriesRunRef, stats: RunStats): SeriesPoint {
    const values: ArmValues = {};
    for (const [armId, a] of Object.entries(stats.arms)) values[armId] = a.passRate;
    return { id: ref.id, startMs: ref.startMs, values };
}

// A run with no readable numbers (an aborted upload) is skipped, not plotted.
async function collectPoints(
    def: SeriesDef,
    runs: readonly SeriesRunRef[],
    want: number,
): Promise<{ ref: SeriesRunRef; stats: RunStats }[]> {
    const out: { ref: SeriesRunRef; stats: RunStats }[] = [];
    const maxScan = Math.min(runs.length, want * SCAN_FACTOR);
    let cursor = 0;
    while (out.length < want && cursor < maxScan) {
        const batch = runs.slice(
            cursor,
            Math.min(cursor + Math.max(want - out.length, MIN_STATS_BATCH), maxScan),
        );
        cursor += batch.length;
        const loaded = await mapWithConcurrency([...batch], STATS_CONCURRENCY, (ref) =>
            loadRunStats(def, ref.id),
        );
        batch.forEach((ref, i) => {
            const stats = loaded[i];
            if (stats) out.push({ ref, stats });
        });
    }
    return out.slice(0, want);
}

// ---------- index row ----------

export interface SeriesIndexRow {
    def: SeriesDef;
    arms: SeriesArm[];
    runCount: number;
    lastRun: SeriesRunRef | null;
    health: SeriesHealth;
    latest: ArmValues;
    gap: number | null;
    sparks: Record<string, (number | null)[]>;
}

export async function loadSeriesIndexRow(
    def: SeriesDef,
    nowMs: number = Date.now(),
): Promise<SeriesIndexRow> {
    const members = await loadSeriesMembers(def);
    const live = plotted(members);
    const loaded = await collectPoints(def, live, SPARK_RUNS);
    const points = loaded.map((p) => pointFromStats(p.ref, p.stats));
    const arms = resolveArms(
        def.arms,
        points.flatMap((p) => Object.keys(p.values)),
    );
    const latest = latestValues(points, arms);
    const oldestFirst = [...points].reverse();
    return {
        def,
        arms,
        runCount: members.length,
        lastRun: live[0] ?? null,
        health: seriesHealth(def, members, nowMs),
        latest,
        gap: armGap(latest, arms),
        sparks: Object.fromEntries(
            arms.map((a) => [a.id, oldestFirst.map((p) => p.values[a.id] ?? null)]),
        ),
    };
}

// ---------- detail ----------

export interface SeriesDetailRun {
    ref: SeriesRunRef;
    stats: RunStats | null;
}

export interface SeriesDetail {
    def: SeriesDef;
    arms: SeriesArm[];
    total: number;
    runs: SeriesDetailRun[];
    points: SeriesPoint[];
}

export async function loadSeriesDetail(
    def: SeriesDef,
    limit: number = DETAIL_RUNS,
): Promise<SeriesDetail> {
    const members = await loadSeriesMembers(def);
    const shown = members.slice(0, limit);
    const loaded = await mapWithConcurrency(shown, STATS_CONCURRENCY, (ref) =>
        loadRunStats(def, ref.id),
    );
    const runs = shown.map((ref, i) => ({ ref, stats: loaded[i] }));
    const arms = resolveArms(
        def.arms,
        runs.flatMap((r) => Object.keys(r.stats?.arms ?? {})),
    );
    const points = runs.flatMap((r) =>
        !r.ref.excluded && r.stats ? [pointFromStats(r.ref, r.stats)] : [],
    );
    return { def, arms, total: members.length, runs, points };
}
