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
