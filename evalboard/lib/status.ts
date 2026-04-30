// Single source of truth for coder_eval task status categorization.
// Mirrors coder_eval `FinalStatus.category` (src/coder_eval/models/enums.py):
//   SUCCESS -> passed
//   ERROR   -> error
//   anything else (FAILURE, TIMEOUT, MAX_TURNS_EXHAUSTED, …) -> failed
//
// Note: this only categorizes coder_eval task statuses. UI status display
// (e.g. StatusPill) also handles flow execution statuses like "Completed"
// and "Faulted" and uses its own logic.

export type StatusCategory = "passed" | "failed" | "error" | "unknown";

export function statusCategory(status: string | null): StatusCategory {
    if (!status) return "unknown";
    if (status === "SUCCESS") return "passed";
    if (status === "ERROR") return "error";
    return "failed";
}

// Default table sort: failures and errors first, unknowns next, passes last.
export function statusSortRank(status: string | null): number {
    const c = statusCategory(status);
    if (c === "failed" || c === "error") return 0;
    if (c === "passed") return 2;
    return 1;
}
