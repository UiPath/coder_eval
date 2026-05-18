/**
 * Loads the canonical Flow v2 connector library produced by
 * `integrations/generate_connectors.py`.
 *
 * The library lives at `<integrations>/library/` with one JSON file per
 * `(connector-key, action-id, version)` plus an `index.json` that lists every
 * entry. We read `index.json` eagerly to know what's available and lazy-load
 * individual entries on first lookup.
 */
export interface LibraryEntry {
    schemaVersion: string;
    nodeType: string;
    version: string;
    category: string;
    tags: string[];
    connector: {
        key: string;
        /** Human display name from `uip is connectors get`. */
        name?: string;
    };
    operation: {
        name: string;
        objectName: string;
        objectDisplayName?: string;
        httpMethod: string;
        /** Resolved path (no placeholders). Older library entries only. */
        path?: string;
        /** Path with `{placeholder}` segments (e.g. "/{repo}/create_issues"). */
        pathTemplate?: string;
        /** Path/query parameters with displayName, description, references. */
        parameters?: Array<Record<string, unknown>>;
        subType: string;
        supportsStreaming: boolean;
    };
    /** Editor-side display tweaks (e.g. operation label override). */
    display: {
        label: string;
        description: string;
        icon: string;
        iconBackground: string;
        iconBackgroundDark: string;
        operationLabel?: string;
    };
    /** Runtime/connector requirements for this action. */
    runtime: {
        bpmnType: string;
        serviceType: string;
        activityConfigurationVersion: string;
        requiresConnection: boolean;
        requiresFolderKey: boolean;
        /** True iff `is resources describe --operation X` returns empty schemas
         *  without a connection-id. Such entries need a per-flow sidecar with
         *  the connection-specific field list. */
        requiresConnectionForSchema?: boolean;
    };
    inputSchema: {
        fields: unknown[];
    };
    outputSchema: {
        fields: unknown[];
    };
}
export declare class Library {
    private libraryDir;
    private indexByKey;
    private cache;
    constructor(libraryDir: string);
    has(nodeType: string, version: string): boolean;
    /** Returns the canonical entry, or undefined if no match. */
    lookup(nodeType: string, version: string): LibraryEntry | undefined;
    /** All known nodeType@version keys. Useful for diagnostics. */
    keys(): string[];
}
/** Convenience: load the library from the default sibling location.
 *
 * Resolution order:
 *   1. `$FLOW_V2_LIBRARY` env var (used by tests + by callers that keep
 *      the library in a shared cache outside the repo).
 *   2. `../integrations/library/` relative to the package root (legacy).
 */
export declare function loadDefaultLibrary(): Library;
/** The directory `loadDefaultLibrary` resolves to. Exposed so callers
 * (e.g. `v2-to-v1.convertV2ToV1`) can use the same resolution rule for
 * the libraryDir option without re-implementing it. */
export declare function defaultLibraryDir(): string;
//# sourceMappingURL=library.d.ts.map