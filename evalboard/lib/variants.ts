// Variant (experiment arm) semantics, in one dependency-free module.
//
// coder_eval's experiment layer fans a task out across `variants:` and writes
// each arm's output to its own subtree — path_utils.build_task_run_dir gives
// <run_dir>/<variant_id>/<task_id>/<NN>/ — stamping `variant_id` on every
// run.json row. An experiment that declares no variants still writes one arm,
// named DEFAULT_VARIANT_ID, which is why a single-arm run and a multi-arm run
// have the same on-disk shape and why defaulting to it everywhere is exact
// rather than approximate.
//
// This file deliberately imports nothing. The grid and the run view are client
// components, so they cannot reach into lib/blob.ts (node:fs, node:path, the
// Azure SDK) for these; keeping the constant, the guard and the row key here
// lets the server readers and the client renderers agree by construction.

export const DEFAULT_VARIANT_ID = "default";

// A variant id occupies exactly ONE path segment (<runId>/<variantId>/<taskId>),
// so it is held to a stricter rule than a task id: no separator at all. Mirrors
// coder_eval's reports_junit._is_safe_component. run.json rows are untyped and
// may be blob-pulled from elsewhere, and the id reaches both a path.join and a
// blob prefix, so a crafted value must not be able to steer either out of the
// run directory. An internal "/" is never legitimate here — dataset expansion
// nests the TASK id, never the variant.
//
// The character class matches lib/blob.ts's ID_RE on purpose; it is restated
// rather than imported because this module must stay free of node built-ins.
const VARIANT_ID_RE = /^[\w.-]+$/;

export function isValidVariantId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 128 &&
        VARIANT_ID_RE.test(id) &&
        id !== "." &&
        id !== ".."
    );
}

// Identity of a grid row's "task", which is the (variant, task) pair — NOT the
// task id alone. An experiment run emits one row per arm per task, and the arms
// are different measurements of different configurations; folding them together
// would make "9 tasks × 2 arms" read as 9 tasks and let one arm's pass mask the
// other's failure. A run with no variants collapses to `default <taskId>`, one
// key per task, exactly as keying on the task id alone used to give.
//
// The key is length-prefixed rather than joined on a separator character. Both
// ids reach here straight off run.json, which is untyped, so "no id can contain
// the separator" would be an assumption about data this function never sees;
// the length prefix makes the encoding injective for arbitrary strings, so two
// different (arm, task) pairs can never collide into one grid row.
export function taskVariantKey(row: {
    taskId: string;
    variantId?: string | null;
}): string {
    const v = row.variantId ?? DEFAULT_VARIANT_ID;
    return `${v.length}:${v}/${row.taskId}`;
}

// The distinct arms present in a set of rows, sorted for stable rendering.
// Length <= 1 means "not an experiment run" and is the condition every variant
// affordance in the UI hides behind.
export function variantsOf(
    rows: readonly { variantId?: string | null }[],
): string[] {
    const s = new Set<string>();
    for (const r of rows) s.add(r.variantId ?? DEFAULT_VARIANT_ID);
    return [...s].sort();
}
