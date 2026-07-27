// Single source of truth for coder_eval task status categorization.
// Mirrors coder_eval `FinalStatus.category` (src/coder_eval/models/enums.py):
//   SUCCESS              -> passed
//   ERROR / BUILD_FAILED -> error  (BUILD_FAILED is an environment/setup failure)
//   anything else (FAILURE, TIMEOUT, MAX_TURNS_EXHAUSTED, …) -> failed
//
// Note: this only categorizes coder_eval task statuses. UI status display
// (e.g. StatusPill) also handles flow execution statuses like "Completed"
// and "Faulted" and uses its own logic.

import type { TaskResultSummary } from "./runs";

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

// Mean of the non-null values, or null when every value is null (so the cell
// renders "—" instead of a misleading 0). The averaging primitive behind the
// replicate collapse below.
function meanOrNull(values: readonly (number | null)[]): number | null {
    let sum = 0;
    let n = 0;
    for (const v of values) {
        if (v != null) {
            sum += v;
            n += 1;
        }
    }
    return n ? sum / n : null;
}

// Collapse per-replicate rows to one row per task for the run grid. Repeated
// runs of a task share a taskId, so this returns a single row per task whose:
//   - categorical fields (status, replicateIndex/detail link, tags, skill,
//     model, expected_turns, mature flag) come from a REPRESENTATIVE replicate —
//     a passing one when any passed, else the lowest-index one — so the status
//     pill and the "open detail" link both describe one real run; and
//   - quantitative columns (score, duration, cost, turns via actualCommands,
//     tokens) are the MEAN across ALL replicates, so the grid reflects the whole
//     repeat set rather than just the representative run. (Previously every
//     column was the representative's own value, so e.g. cost showed a single
//     run's price instead of the average over the repeats.)
// First-seen task order is preserved. With repeats disabled (one replicate per
// task) each mean is that single value, so the output is byte-identical.
export function collapseReplicates(
    rows: readonly TaskResultSummary[],
): TaskResultSummary[] {
    const groups = new Map<string, TaskResultSummary[]>();
    for (const t of rows) {
        const g = groups.get(t.taskId);
        if (g) g.push(t);
        else groups.set(t.taskId, [t]);
    }
    const out: TaskResultSummary[] = [];
    for (const group of groups.values()) {
        // Representative for the categorical fields: a passing replicate wins
        // over a non-passing one; ties break to the lowest replicateIndex.
        let rep = group[0];
        for (const t of group) {
            const repPass = isPassStatus(rep.status);
            const tPass = isPassStatus(t.status);
            if (repPass !== tPass) {
                if (tPass) rep = t;
            } else if ((t.replicateIndex ?? 0) < (rep.replicateIndex ?? 0)) {
                rep = t;
            }
        }
        out.push({
            ...rep,
            weightedScore: meanOrNull(group.map((t) => t.weightedScore)),
            durationSeconds: meanOrNull(group.map((t) => t.durationSeconds)),
            totalCostUsd: meanOrNull(group.map((t) => t.totalCostUsd)),
            // Turns render from displayedTurns(actualCommands, hasFinalReply);
            // averaging the command count carries the average into that column.
            actualCommands: meanOrNull(group.map((t) => t.actualCommands)),
            totalTurns: meanOrNull(group.map((t) => t.totalTurns)),
            inputTokens: meanOrNull(group.map((t) => t.inputTokens)),
            outputTokens: meanOrNull(group.map((t) => t.outputTokens)),
            cacheCreationTokens: meanOrNull(
                group.map((t) => t.cacheCreationTokens),
            ),
            cacheReadTokens: meanOrNull(group.map((t) => t.cacheReadTokens)),
        });
    }
    return out;
}

// Default table sort: failures and errors first, unknowns next, passes last.
export function statusSortRank(status: string | null): number {
    const c = statusCategory(status);
    if (c === "failed" || c === "error") return 0;
    if (c === "passed") return 2;
    return 1;
}
