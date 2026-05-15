/**
 * Distill the v1 `inputs.detail.configuration` JSON-string blob into a
 * minimal per-instance form for Flow v2.
 *
 * Empirical analysis of curated-connector configurations (see findings in
 * the design discussion) shows that across all observed instances, every
 * field falls into one of three buckets:
 *
 *   Tier A — globally constant defaults (15 fields). Strip; restore from
 *            the DEFAULTS table on v2→v1.
 *   Tier B — per-connector statics (some library-derivable, some only
 *            available with a connection-id-enriched registry call). Strip
 *            when the value matches what the canonical library already
 *            knows; keep otherwise.
 *   Tier C — `fieldsContainer` (16–51 KB editor-cache mirror of the
 *            connector's input/output schemas). Strip when its content
 *            matches the per-connector sidecar cache; keep verbatim
 *            otherwise. The cache is populated opportunistically: the
 *            FIRST flow we convert that uses a given (connector, action)
 *            captures the blob; subsequent conversions strip it.
 *
 * Anything that doesn't match a tier rule is preserved in an `extras`
 * object on the manifest entry so we never lose data on unfamiliar
 * connectors or future editor versions.
 */
import { LibraryEntry } from './library';
export declare const TIER_A_DEFAULTS: Record<string, unknown>;
interface FieldsContainerCacheEntry {
    hash: string;
    size: number;
    capturedFrom: string;
    capturedAt: string;
    blob: unknown;
}
export interface FieldsContainerCache {
    /** Resolves a cached fieldsContainer for a given typeRef. */
    get(typeRef: string): FieldsContainerCacheEntry | undefined;
    /** Records a new fieldsContainer for a typeRef (only if not already cached). */
    put(typeRef: string, blob: unknown, source: string): boolean;
}
export declare class FileSystemFieldsCache implements FieldsContainerCache {
    private cacheDir;
    private memo;
    constructor(cacheRoot: string);
    private pathFor;
    get(typeRef: string): FieldsContainerCacheEntry | undefined;
    put(typeRef: string, blob: unknown, source: string): boolean;
}
export interface DistilledConfiguration {
    /** Per-instance fields that survive distillation. */
    kept: Record<string, unknown>;
    /** Fields that didn't fit any rule, preserved verbatim for safety. */
    extras: Record<string, unknown>;
    /** Whether `fieldsContainer` was stripped (true) or kept (false). */
    fieldsContainerStripped: boolean;
    /** Diagnostic counters. */
    stats: {
        tierA: number;
        tierB: number;
        tierC: number;
        extras: number;
    };
}
export interface DistillOptions {
    /** Sidecar cache. If absent, fieldsContainer is preserved verbatim. */
    cache?: FieldsContainerCache;
    /** Source identifier for cache provenance. */
    source?: string;
    /** typeRef (`<nodeType>@<version>`) for the node being distilled. */
    typeRef: string;
    /** Canonical library entry; required for Tier B stripping. */
    library?: LibraryEntry;
}
export declare function distillConfiguration(config: Record<string, unknown>, opts: DistillOptions): DistilledConfiguration;
/** Convenience: load the default sidecar cache from integrations/cache/. */
export declare function loadDefaultFieldsCache(): FileSystemFieldsCache;
export {};
//# sourceMappingURL=configuration.d.ts.map