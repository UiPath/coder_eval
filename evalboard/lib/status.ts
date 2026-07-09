// Single source of truth for coder_eval task status categorization.
// Mirrors coder_eval `FinalStatus.category` (src/coder_eval/models/enums.py):
//   SUCCESS              -> passed
//   ERROR / BUILD_FAILED -> error  (BUILD_FAILED is an environment/setup failure)
//   anything else (FAILURE, TIMEOUT, MAX_TURNS_EXHAUSTED, …) -> failed
//
// Note: this only categorizes coder_eval task statuses. UI status display
// (e.g. StatusPill) also handles flow execution statuses like "Completed"
// and "Faulted" and uses its own logic.

export type StatusCategory = "passed" | "failed" | "error" | "unknown";

export function statusCategory(status: string | null): StatusCategory {
    if (!status) return "unknown";
    if (status === "SUCCESS") return "passed";
    if (status === "ERROR" || status === "BUILD_FAILED") return "error";
    return "failed";
}

// Whether a status is a pass (SUCCESS). The single predicate behind the
// "a task passes if any replicate passed" rule.
export function isPassStatus(status: string | null): boolean {
    return statusCategory(status) === "passed";
}

// Roll per-replicate rows up per task: taskId -> number of replicates that
// passed. Repeated runs share a taskId, so this is the one place the "any
// replicate passed" aggregation lives — consumed by the run-page pass-rate
// tile AND the grid badge / collapse so they can never disagree.
export function perTaskPassCounts<
    T extends { taskId: string; status: string | null },
>(rows: readonly T[]): Map<string, number> {
    const m = new Map<string, number>();
    for (const r of rows) {
        m.set(r.taskId, (m.get(r.taskId) ?? 0) + (isPassStatus(r.status) ? 1 : 0));
    }
    return m;
}

// Default table sort: failures and errors first, unknowns next, passes last.
export function statusSortRank(status: string | null): number {
    const c = statusCategory(status);
    if (c === "failed" || c === "error") return 0;
    if (c === "passed") return 2;
    return 1;
}
