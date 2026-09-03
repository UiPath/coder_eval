// Single source of truth for coder_eval task status categorization.
// Mirrors coder_eval `FinalStatus.category` (src/coder_eval/models/enums.py):
//   SUCCESS              -> passed
//   ERROR / BUILD_FAILED -> error  (BUILD_FAILED is an environment/setup failure)
//   NOT_GRADED           -> ungraded (`coder-eval execute`: ran, deliberately unscored)
//   anything else (FAILURE, TIMEOUT, MAX_TURNS_EXHAUSTED, …) -> failed
//
// "ungraded" is its OWN member rather than being folded into "unknown". Folding
// it there looks safe — an ungraded row genuinely has no verdict — but every
// rate helper in this app is written as `if passed … else if error … else
// failed++`, so anything that is not a pass or an error is counted as a failure
// AND kept in the denominator. A clean `execute` run then renders as 0% pass, N
// failed. A distinct member makes that a type error at each site instead, so a
// consumer has to decide what to do with it.
//
// Note: this only categorizes coder_eval task statuses. UI status display
// (e.g. StatusPill) also handles flow execution statuses like "Completed"
// and "Faulted" and uses its own logic.

import { taskVariantKey } from "./variants";

export type StatusCategory = "passed" | "failed" | "error" | "ungraded" | "unknown";

export function statusCategory(status: string | null): StatusCategory {
    if (!status) return "unknown";
    if (status === "SUCCESS") return "passed";
    if (status === "ERROR" || status === "BUILD_FAILED") return "error";
    if (status === "NOT_GRADED") return "ungraded";
    return "failed";
}

// Whether a row was measured at all. An ungraded row must leave BOTH sides of
// every rate — it is not a pass and not a failure, so counting it either way
// (or keeping it in a denominator) misreports a run that was never scored.
export function isGraded(status: string | null): boolean {
    return statusCategory(status) !== "ungraded";
}

// Whether a status is a pass (SUCCESS). The single predicate behind the
// "a task passes if any replicate passed" rule.
export function isPassStatus(status: string | null): boolean {
    return statusCategory(status) === "passed";
}

// Roll per-replicate rows up per (variant, task): key -> number of replicates
// that passed. Repeated runs share a taskId, so this is the one place the "any
// replicate passed" aggregation lives — consumed by the run-page pass-rate
// tile AND the grid badge / collapse so they can never disagree. Key the lookup
// with taskVariantKey.
export function perTaskPassCounts<
    T extends { taskId: string; variantId?: string | null; status: string | null },
>(rows: readonly T[]): Map<string, number> {
    const m = new Map<string, number>();
    for (const r of rows) {
        const k = taskVariantKey(r);
        m.set(k, (m.get(k) ?? 0) + (isPassStatus(r.status) ? 1 : 0));
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
