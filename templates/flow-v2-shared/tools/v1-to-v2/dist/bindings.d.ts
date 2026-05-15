/**
 * Extract v1 bindings[] into a stand-alone bindings.json.
 *
 * The user's existing agent flow already produces bindings.json as a
 * separate file; we mirror that shape here. The v1 bindings[] entries
 * carry a fixed structure:
 *
 *   { id, name, type, resource, resourceKey, default, propertyAttribute,
 *     resourceSubType? }
 *
 * Most of those map 1:1 to v2 — we just lift them out of the .flow file
 * and into a sibling document. The manifest references them by their
 * original `id` (e.g. `bt2G1ynya`) so v1↔v2 round-trip is positional and
 * lossless.
 */
export interface BindingsFile {
    schemaVersion: string;
    bindings: BindingEntry[];
}
export interface BindingEntry {
    id: string;
    name: string;
    type: string;
    resource: string;
    resourceKey?: string;
    default?: unknown;
    propertyAttribute?: string;
    resourceSubType?: string;
    [extra: string]: unknown;
}
export declare function buildBindings(rawBindings: unknown[]): BindingsFile;
//# sourceMappingURL=bindings.d.ts.map