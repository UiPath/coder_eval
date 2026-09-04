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
// failed.
//
// Widening this union does NOT by itself produce a type error at the consumers:
// most of them go through `isPassStatus` / `isGraded` rather than switching on
// StatusCategory, and the two that do switch used if/else chains with a final
// `else`. That is exactly why the first ungraded sweep missed lib/overview.ts
// entirely. `assertNever` below is the fix: a consumer that must handle every
// category exhaustively uses a `switch` with `default: return assertNever(c)`,
// and adding a member then fails `tsc --noEmit` at that site.
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
        // Ungraded replicates leave BOTH sides, exactly as they do in every
        // per-row rate: they are excluded from the count AND from the map, so a
        // task whose replicates were all ungraded does not appear at all rather
        // than appearing as "0 of N passed". Without this an
        // `execute --repeats 2` run renders a red "0/2 ✓".
        if (!isGraded(r.status)) continue;
        const k = taskVariantKey(r);
        m.set(k, (m.get(k) ?? 0) + (isPassStatus(r.status) ? 1 : 0));
    }
    return m;
}

// Per (variant, task): how many replicates were GRADED. The denominator paired
// with perTaskPassCounts, so a badge reading "k/N ✓" never divides a graded
// numerator by an all-rows N.
export function perTaskGradedCounts<
    T extends { taskId: string; variantId?: string | null; status: string | null },
>(rows: readonly T[]): Map<string, number> {
    const m = new Map<string, number>();
    for (const r of rows) {
        if (!isGraded(r.status)) continue;
        const k = taskVariantKey(r);
        m.set(k, (m.get(k) ?? 0) + 1);
    }
    return m;
}

// Compile-time exhaustiveness guard. Call it from a `switch`'s `default` arm
// over a StatusCategory: a new member then makes the argument non-`never` and
// `tsc --noEmit` fails at that site, which is what forces every consumer to be
// revisited instead of silently falling through an `else`.
export function assertNever(x: never): never {
    throw new Error(`Unhandled status category: ${String(x)}`);
}

// Default table sort: failures and errors first, ungraded/unknown next, passes
// last. A `switch` with `assertNever`, not the if/else-with-catch-all this
// file's own header calls out: the catch-all sorted the new "ungraded" bucket
// into the "unknown" rank by fall-through rather than by decision, and a sixth
// category would land there just as silently.
export function statusSortRank(status: string | null): number {
    const c = statusCategory(status);
    switch (c) {
        case "failed":
        case "error":
            return 0;
        case "passed":
            return 2;
        case "ungraded":
        case "unknown":
            // Between the two: an ungraded row is not a failure to rank first,
            // and not a pass to bury last.
            return 1;
        default:
            return assertNever(c);
    }
}
