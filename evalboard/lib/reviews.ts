import { promises as fs } from "node:fs";
import path from "node:path";
import { unstable_cache } from "next/cache";
import { ensureRunReviewIndex } from "./blob";
import { RUNS_DIR, listRunIds } from "./runs";
import type {
    Review,
    ReviewIndex,
    ReviewIndexEntry,
    TaskAggregate,
    TaskRunHit,
    Window,
} from "./reviews-types";

export type {
    Review,
    ReviewIndex,
    ReviewIndexEntry,
    TaskAggregate,
    TaskRunHit,
    Window,
} from "./reviews-types";
export { WINDOWS } from "./reviews-types";

// ---------- Per-run reads ----------

// Returns null when the digest is absent (older runs predate the feature).
export async function readRunReviewIndex(
    runId: string,
): Promise<ReviewIndex | null> {
    await ensureRunReviewIndex(runId, RUNS_DIR);
    const p = path.join(RUNS_DIR, runId, "review_index.json");
    let raw: string;
    try {
        raw = await fs.readFile(p, "utf-8");
    } catch {
        return null;
    }
    try {
        return JSON.parse(raw) as ReviewIndex;
    } catch {
        return null;
    }
}

// Per-task review.json — used on the task-detail page. ensureTaskDir already
// pulls the entire task subtree, so the file is on disk by the time this runs.
export async function readTaskReview(
    runId: string,
    variantId: string,
    taskId: string,
    replicate: string,
): Promise<Review | null> {
    const p = path.join(
        RUNS_DIR,
        runId,
        variantId,
        taskId,
        replicate,
        "review.json",
    );
    let raw: string;
    try {
        raw = await fs.readFile(p, "utf-8");
    } catch {
        return null;
    }
    try {
        return JSON.parse(raw) as Review;
    } catch {
        return null;
    }
}

export function tagCountsForRun(
    index: ReviewIndex,
): { tag: string; count: number }[] {
    const counts = new Map<string, number>();
    for (const e of index.reviews) {
        for (const t of e.tags) counts.set(t, (counts.get(t) ?? 0) + 1);
    }
    return [...counts.entries()]
        .map(([tag, count]) => ({ tag, count }))
        .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

// Per-task review entries indexed by task_id (collapsed across variant/replicate
// for the run grid — first occurrence wins for stable rendering).
export type EntriesByTask = Map<string, ReviewIndexEntry>;

export function indexByTask(index: ReviewIndex): EntriesByTask {
    const out: EntriesByTask = new Map();
    for (const e of index.reviews) {
        if (!out.has(e.task_id)) out.set(e.task_id, e);
    }
    return out;
}

// ---------- Hotspots: cross-run aggregation ----------

const WINDOW_DAYS: Record<Window, number> = {
    "1d": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
};

const RUN_ID_RE = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$/;

export function parseRunIdDate(runId: string): Date | null {
    const m = RUN_ID_RE.exec(runId);
    if (!m) return null;
    const [, y, mo, d, h, mi, s] = m;
    const t = Date.UTC(
        Number(y),
        Number(mo) - 1,
        Number(d),
        Number(h),
        Number(mi),
        Number(s),
    );
    return Number.isFinite(t) ? new Date(t) : null;
}

export async function listRunIdsInWindow(window: Window): Promise<string[]> {
    const days = WINDOW_DAYS[window];
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    const ids = await listRunIds();
    return ids.filter((id) => {
        const t = parseRunIdDate(id);
        return t != null && t.getTime() >= cutoff;
    });
}

const FETCH_CONCURRENCY = 16;
const DOMINANT_TAG_LIMIT = 4;

async function mapWithConcurrency<T, U>(
    items: T[],
    limit: number,
    fn: (item: T) => Promise<U>,
): Promise<U[]> {
    const out: U[] = new Array(items.length);
    let cursor = 0;
    async function worker() {
        while (true) {
            const i = cursor++;
            if (i >= items.length) return;
            out[i] = await fn(items[i]);
        }
    }
    await Promise.all(
        Array.from({ length: Math.min(limit, items.length) }, worker),
    );
    return out;
}

interface PerRun {
    runId: string;
    index: ReviewIndex | null;
}

async function fetchReviewIndices(window: Window): Promise<PerRun[]> {
    const ids = await listRunIdsInWindow(window);
    return mapWithConcurrency(ids, FETCH_CONCURRENCY, async (id) => ({
        runId: id,
        index: await readRunReviewIndex(id),
    }));
}

function aggregateTasks(perRun: PerRun[]): TaskAggregate[] {
    type Bucket = {
        occurrences: number;
        runs: Set<string>;
        tagCounts: Map<string, number>;
        lastSeenRunId: string;
    };
    const buckets = new Map<string, Bucket>();
    for (const { runId, index } of perRun) {
        if (!index) continue;
        for (const e of index.reviews) {
            let b = buckets.get(e.task_id);
            if (!b) {
                b = {
                    occurrences: 0,
                    runs: new Set(),
                    tagCounts: new Map(),
                    lastSeenRunId: runId,
                };
                buckets.set(e.task_id, b);
            }
            b.occurrences += 1;
            b.runs.add(runId);
            // run_ids are timestamped YYYY-MM-DD_HH-MM-SS so lexical max == newest.
            if (runId > b.lastSeenRunId) b.lastSeenRunId = runId;
            for (const tag of e.tags) {
                b.tagCounts.set(tag, (b.tagCounts.get(tag) ?? 0) + 1);
            }
        }
    }
    return [...buckets.entries()]
        .map(([taskId, b]) => ({
            taskId,
            occurrences: b.occurrences,
            affectedRuns: b.runs.size,
            dominantTags: [...b.tagCounts.entries()]
                .map(([tag, count]) => ({ tag, count }))
                .sort(
                    (a, b) =>
                        b.count - a.count || a.tag.localeCompare(b.tag),
                )
                .slice(0, DOMINANT_TAG_LIMIT),
            lastSeenRunId: b.lastSeenRunId,
        }))
        .sort(
            (a, b) =>
                b.affectedRuns - a.affectedRuns ||
                b.occurrences - a.occurrences ||
                a.taskId.localeCompare(b.taskId),
        );
}

export async function aggregateRecentTasks(
    window: Window,
): Promise<TaskAggregate[]> {
    const cached = unstable_cache(
        async () => aggregateTasks(await fetchReviewIndices(window)),
        ["aggregate-recent-tasks", window],
        { revalidate: 300 },
    );
    return cached();
}

export async function runsForTask(
    taskId: string,
    window: Window,
): Promise<TaskRunHit[]> {
    const cached = unstable_cache(
        async () => {
            const perRun = await fetchReviewIndices(window);
            const hits: TaskRunHit[] = [];
            for (const { runId, index } of perRun) {
                if (!index) continue;
                for (const e of index.reviews) {
                    if (e.task_id !== taskId) continue;
                    hits.push({
                        runId,
                        variantId: e.variant_id,
                        replicate: e.replicate,
                        tags: e.tags,
                        summaryExcerpt: e.summary_excerpt ?? "",
                    });
                }
            }
            // Newest first — matches the lex-sortable run_id format.
            hits.sort((a, b) => b.runId.localeCompare(a.runId));
            return hits;
        },
        ["runs-for-task", taskId, window],
        { revalidate: 300 },
    );
    return cached();
}
