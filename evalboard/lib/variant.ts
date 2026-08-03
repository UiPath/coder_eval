// Variant (experiment / model) primitives shared across the dashboard.
//
// A "variant" is the on-disk subdir a run writes each task under
// (<run>/<variant>/<task>/<NN>/). A single-config run uses the literal
// "default"; an A/B (multi-model) run uses one variant per arm, e.g. "kimi-k3".
// The URL carries it as ?v=<variant>, mirroring ?r=<replicate>.
//
// This module is a dependency-free LEAF on purpose: it holds only the constant
// and pure URL helpers, so it is safe to import from client components
// (task-grid.tsx) and the client-safe status.ts. The PATH sanitizer that turns
// a variant into a validated filesystem segment (`variantSegment`) lives in
// lib/runs.ts instead — it depends on isValidId (which pulls the node-only blob
// layer), so it must stay server-side.

// The subdir a single-config run writes its tasks under, and the safe fallback
// for legacy rows (no variant recorded) and for a missing ?v=. SINGLE SOURCE —
// import this instead of re-typing the "default" literal.
export const DEFAULT_VARIANT = "default";

// Next's App Router types a query value as `string | string[] | undefined` — a
// repeated key (`?v=a&v=b`) yields an array. Collapse to the first value so a
// repeated param can't reach a `path.join` as an array (which throws a 500).
export function firstParam(
    v: string | string[] | null | undefined,
): string | undefined {
    const first = Array.isArray(v) ? v[0] : v;
    return first != null && first.length > 0 ? first : undefined;
}

// Normalize a raw ?v= query value into the variant to read. Absent / empty →
// "default". Also collapses a repeated param to its first value. The path
// readers additionally run variantSegment() to reject unsafe values.
export function variantFromParam(
    v: string | string[] | null | undefined,
): string {
    return firstParam(v) ?? DEFAULT_VARIANT;
}

// The query-string fragment that preserves the current variant on in-page links
// (the replicate selector, the download link, cross-page task links). Empty for
// the default variant so single-config URLs stay clean and unchanged. Returns a
// leading "&" so it appends after an existing query (`?r=2${variantLinkParam(v)}`).
export function variantLinkParam(variant: string | null | undefined): string {
    return variant && variant !== DEFAULT_VARIANT
        ? `&v=${encodeURIComponent(variant)}`
        : "";
}
