import { promises as fs } from "node:fs";
import path from "node:path";
import { ensureRunReviewIndex } from "./blob";
import { RUNS_DIR, listRunIds, variantSegment } from "./runs";
import { taskGroupKey } from "./status";
import type {
    Review,
    ReviewIndex,
    ReviewIndexEntry,
    Window,
} from "./reviews-types";

export type {
    Review,
    ReviewIndex,
    ReviewIndexEntry,
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
    // Sanitize the variant here too — this is a path-consuming reader like the
    // others, so it must not trust its caller (page.tsx forwards the run's arm,
    // but a raw ?v= must never reach path.join). variantSegment collapses "."/
    // ".." / any non-id to "default".
    const p = path.join(
        RUNS_DIR,
        runId,
        variantSegment(variantId),
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

// Per-arm review entries keyed by (task_id, variant) via taskGroupKey — the SAME
// key the grid rows collapse on — so in an A/B run each arm shows its OWN review
// instead of both arms inheriting the first arm's summary/tags. Collapsed across
// replicate only (first occurrence per arm wins for stable rendering). Look rows
// up with taskGroupKey(row), not row.taskId.
export type EntriesByTask = Map<string, ReviewIndexEntry>;

export function indexByTask(index: ReviewIndex): EntriesByTask {
    const out: EntriesByTask = new Map();
    for (const e of index.reviews) {
        // Empty variant_id (legacy indexes) → null so it collapses to "default",
        // matching a row whose variant is null/"default".
        const key = taskGroupKey({
            taskId: e.task_id,
            variant: e.variant_id || null,
        });
        if (!out.has(key)) out.set(key, e);
    }
    return out;
}

// ---------- Window helpers ----------

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
