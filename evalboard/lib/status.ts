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
import { DEFAULT_VARIANT } from "./variant";

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

// Separator used to join a task id and its variant into one map key. A control
// character (unit separator) that can never appear in a task id or variant id,
// so the two segments can never collide.
const KEY_SEP = "\u001f";

// Grouping key for the "one row per task" collapse. A task's repeated runs
// (replicates) share it, so they fold together; but in a multi-model (A/B) run
// several variants ALSO share a taskId — they must stay DISTINCT rows, one per
// model — so the variant is part of the key. Single-config runs carry variant
// "default" (or null on legacy rows), so the key is effectively the taskId and
// behavior is unchanged.
// `variant` is REQUIRED (matching TaskResultSummary.variant, a required
// `string | null`) — declaring it optional would let a row-shaped object that
// forgot the field type-check and silently fold every arm of an A/B run back
// into one "default" group, which is exactly the bug this key prevents. The
// `?? DEFAULT_VARIANT` still covers the legacy-null case.
export function taskGroupKey(t: {
    taskId: string;
    variant: string | null;
}): string {
    return `${t.taskId}${KEY_SEP}${t.variant ?? DEFAULT_VARIANT}`;
}

// Roll per-replicate rows up per (task, variant): key -> number of replicates
// that passed. Repeated runs share a key, so this is the one place the "any
// replicate passed" aggregation lives — consumed by the run-page pass-rate
// tile AND the grid badge / collapse so they can never disagree. Keyed by
// taskGroupKey so a multi-model run counts each model's attempt separately.
export function perTaskPassCounts<
    T extends { taskId: string; status: string | null; variant: string | null },
>(rows: readonly T[]): Map<string, number> {
    const m = new Map<string, number>();
    for (const r of rows) {
        const k = taskGroupKey(r);
        m.set(k, (m.get(k) ?? 0) + (isPassStatus(r.status) ? 1 : 0));
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

// Collapse per-replicate rows to one row per (task, variant) for the run grid.
// Repeated runs of a task share a taskId, so they fold together; a multi-model
// run keeps one row PER MODEL (the variant is part of the group key), so each
// model's metrics stay separate instead of being averaged across models. Each
// collapsed row's:
//   - categorical + verdict fields (status, weightedScore, replicateIndex/detail
//     link, tags, skill, model, variant, expected_turns, mature flag) come from
//     a REPRESENTATIVE replicate — a passing one when any passed, else the
//     lowest-index one — so the Status pill, the Score, and the "open detail"
//     link ALL describe the SAME run (clicking a "Passed" row lands on a page
//     showing that same score); and
//   - resource columns (duration, cost, turns via actualCommands, tokens) are
//     the MEAN across ALL replicates, so the grid reflects the whole repeat set
//     rather than just the representative run. (Previously cost/tokens/etc.
//     showed a single run's value instead of the average over the repeats.)
// weightedScore is DELIBERATELY kept on the representative, not averaged: the row
// shows one pass/fail verdict, so its score must be that run's score — an
// averaged score beside a representative Status pill reads "Passed · 0.60" for a
// SUCCESS/FAILURE pair and then shows 1.00 on click-through. Score aggregation,
// if wanted, belongs in a separately labeled column.
// First-seen group order is preserved. With repeats disabled (one replicate per
// task/variant) each mean is that single value, so the output is byte-identical.
export function collapseReplicates(
    rows: readonly TaskResultSummary[],
): TaskResultSummary[] {
    const groups = new Map<string, TaskResultSummary[]>();
    for (const t of rows) {
        const key = taskGroupKey(t);
        const g = groups.get(key);
        if (g) g.push(t);
        else groups.set(key, [t]);
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
            // weightedScore intentionally NOT averaged — see the note above; it
            // stays the representative's so Status/Score/detail-link agree.
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
