// Types for the per-task review.json artifact + run-level review_index.json
// digest. Safe to import from client components — no node:fs/node:path here.

export interface Review {
    task_id: string;
    summary: string;
    tags: string[];
    created_at: string;
}

export interface ReviewIndexEntry {
    task_id: string;
    variant_id: string;
    replicate: string;
    tags: string[];
    summary_excerpt?: string;
}

export interface ReviewIndex {
    generated_at: string;
    reviews: ReviewIndexEntry[];
}

export type Window = "1d" | "7d" | "14d" | "30d";

export const WINDOWS: Window[] = ["1d", "7d", "14d", "30d"];

// Hotspots aggregate by task_id, not by tag. Open-vocabulary tags drift
// across days, so cross-day tag aggregation is noisy; the task axis is
// stable and the most actionable signal ("which tasks fail repeatedly").
export interface TaskAggregate {
    taskId: string;
    occurrences: number; // total review entries (sums across replicates)
    affectedRuns: number; // distinct run_ids in window
    dominantTags: { tag: string; count: number }[]; // sorted desc
    lastSeenRunId: string; // newest run_id where this task has a review
}

export interface TaskRunHit {
    runId: string;
    variantId: string;
    replicate: string;
    tags: string[];
    summaryExcerpt: string;
}
