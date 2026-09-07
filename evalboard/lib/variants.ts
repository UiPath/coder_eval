// Variant (experiment arm) semantics, in one dependency-free module.
//
// coder_eval writes each arm to its own subtree — <run>/<variant>/<task>/<NN>/ —
// and stamps `variant_id` on every run.json row. An experiment declaring no
// variants still writes one arm named DEFAULT_VARIANT_ID, so single-arm and
// multi-arm runs have the same shape on disk.
//
// Imports nothing on purpose: the grid and run view are client components and
// cannot reach into lib/blob.ts (node:fs, the Azure SDK) for these.

export const DEFAULT_VARIANT_ID = "default";

// A variant id is exactly ONE path segment, so it is held to a stricter rule
// than a task id, which may nest (`<suite>/<row>` from dataset expansion).
// Mirrors coder_eval's reports_junit._is_safe_component. Restated rather than
// imported from lib/blob.ts to keep this module free of node built-ins.
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

// A grid row's identity is the (variant, task) pair, not the task id: folding
// arms together would let one arm's pass mask the other's failure.
//
// Length-prefixed rather than joined on a separator, because both ids arrive
// straight off untyped run.json — "neither can contain the separator" would be
// an assumption about data this function never sees.
export function taskVariantKey(row: {
    taskId: string;
    variantId?: string | null;
}): string {
    const v = row.variantId ?? DEFAULT_VARIANT_ID;
    return `${v.length}:${v}/${row.taskId}`;
}

// The distinct arms in a set of rows, sorted for stable rendering. Length <= 1
// is what every variant affordance in the UI hides behind.
export function variantsOf(
    rows: readonly { variantId?: string | null }[],
): string[] {
    const s = new Set<string>();
    for (const r of rows) s.add(r.variantId ?? DEFAULT_VARIANT_ID);
    return [...s].sort();
}
