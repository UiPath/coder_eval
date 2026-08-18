import { DEFAULT_SOURCE } from "@/lib/sources";

// Every run-scoped surface carries its source in this query param. Run ids are
// only unique WITHIN a blob container (both suites name runs
// `YYYY-MM-DD_HH-MM-SS`), so a link that drops it doesn't 404 — it silently
// renders a DIFFERENT run's data under the default source.
export const SRC_PARAM = "src";

/** Normalize a repeated query param to the scalar the readers expect. */
export function scalarParam(
    raw: string | string[] | undefined,
): string | undefined {
    return Array.isArray(raw) ? raw[0] : raw;
}

/**
 * Append the source token to an internal href.
 *
 * The default source is omitted so every pre-existing URL stays byte-identical
 * (and shareable links don't grow a param that means "the default").
 */
export function withSource(href: string, sourceId?: string | null): string {
    if (!sourceId || sourceId === DEFAULT_SOURCE.id) return href;
    // Split the fragment off first: appending to "/runs/r1#section" would yield
    // "#section?src=…", which a browser reads as fragment TEXT, not a query —
    // so the link would silently fall back to the default source and render a
    // different container's run. Latent today (no caller passes a fragment) but
    // the failure is invisible, which is exactly the class of bug the source
    // param exists to close.
    const hash = href.indexOf("#");
    const base = hash === -1 ? href : href.slice(0, hash);
    const frag = hash === -1 ? "" : href.slice(hash);
    const sep = base.includes("?") ? "&" : "?";
    return `${base}${sep}${SRC_PARAM}=${encodeURIComponent(sourceId)}${frag}`;
}
