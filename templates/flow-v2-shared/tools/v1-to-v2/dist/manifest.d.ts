/**
 * Build the in-memory Flow v2 manifest from collected FlowOverrides
 * + the canonical connector library.
 *
 * The manifest is now an intermediate: it carries the varying parts of each
 * node (per-instance config, bindings, ui) while delegating the static parts
 * (BPMN model, output schema, icons, descriptions) to the library. The
 * converter then bakes this data into FIL action/trigger declarations.
 */
import { FlowOverrides, WorkflowVariables } from './flow-types';
import { Library } from './library';
import { FieldsContainerCache } from './configuration';
export interface ManifestFile {
    schemaVersion: string;
    flowId: string;
    flowName: string;
    flowVersion: string;
    solutionId?: string;
    projectId?: string;
    description?: string;
    /** Per-node implementation entries, keyed by node ID (matches FIL executeNode("<id>", ...)). */
    nodes: Record<string, ManifestNode>;
    /** Variables section is preserved verbatim from v1 — same shape, same semantics. */
    variables?: WorkflowVariables;
    /** Subflows are preserved verbatim for now (TODO: convert nested flows to v2 too). */
    subflows?: Record<string, unknown>;
    /**
     * v1 `definitions[]` entries for typeRefs the canonical library doesn't
     * cover — tenant-specific types like `uipath.core.agent.<uuid>` that can
     * never be in a global library, plus any system types
     * (`core.action.script`, `core.logic.merge`, …) until they're added to
     * the library generator. Keyed by `<nodeType>@<version>`. v2→v1 reads
     * here as a fallback when the library lookup misses.
     */
    embeddedDefinitions?: Record<string, unknown>;
}
export interface ManifestNode {
    /** Reference to the canonical library entry, e.g. uipath.connector.X.Y@1.0.0. */
    type: string;
    /** Optional per-instance display label override. Omitted when it equals the library default. */
    label?: string;
    /** Editor-only positioning. Optional; only present if the editor placed the node. */
    ui?: {
        position: {
            x: number;
            y: number;
        };
        size?: {
            width: number;
            height: number;
        };
    };
    /**
     * Connection binding — the symbolic ID of a bindings.json entry (e.g.
     * "bOutlook"). The real ConnectionId UUID lives in that entry's
     * resourceKey/default. Omitted if the library entry says no connection is
     * required.
     */
    binding?: string;
    /**
     * Folder-key binding — symbolic ID of a bindings.json entry whose
     * propertyAttribute is "FolderKey". The real FolderKey UUID lives in that
     * entry's resourceKey/default.
     */
    folderBinding?: string;
    /** Per-instance varying input values that survived configuration distillation. */
    inputs?: Record<string, unknown>;
    /**
     * Distilled `inputs.detail.configuration` payload. Holds only the fields
     * a v2→v1 reconstruction can't synthesise from the canonical library or
     * the documented defaults. Empty object means the configuration was fully
     * library-derivable (the common case).
     */
    configuration?: Record<string, unknown>;
    /**
     * Catch-all for configuration fields whose values diverged from the
     * library's expected value or the global default. Preserved verbatim so
     * v2→v1 round-trips losslessly even on unfamiliar connectors.
     */
    configurationExtras?: Record<string, unknown>;
    /** Per-instance output bindings (which flow variable receives which output). */
    outputs?: Record<string, {
        source?: string;
        var?: string;
    }>;
    /** For non-integration nodes: the raw v1 inputs blob, since they have no library entry. */
    rawInputs?: Record<string, unknown>;
    /** Process-style resource metadata for non-IS nodes such as Agents, API Workflows, and RPA Workflows. */
    resource?: ManifestResource;
    /** Symbolic bindings.json references for process-style resource properties. */
    resourceBindings?: ManifestResourceBindings;
    /**
     * For `core.logic.mock` nodes and process-resource dispatchers in `--dry-run` mode:
     * the JSON value the dispatcher returns when the node fires. Promoted to
     * a top-level body field so the FIL author doesn't have to nest it inside
     * `rawInputs.fixture` (the legacy v1 shape, which v1-to-v2 still reads
     * and promotes here). Any JSON value.
     */
    fixture?: unknown;
}
export interface ManifestResource {
    resource: string;
    resourceSubType?: string;
    resourceKey?: string;
    orchestratorType?: string;
    serviceType?: string;
    section?: string;
}
export interface ManifestResourceBindings {
    name?: string;
    folderPath?: string;
    [propertyAttribute: string]: string | undefined;
}
export interface BuildOptions {
    /** When true, attach a `_diagnostics` array to the manifest documenting compression decisions. */
    includeDiagnostics?: boolean;
    /** Sidecar cache for `fieldsContainer` blobs. */
    fieldsCache?: FieldsContainerCache;
    /** Source identifier used when capturing fieldsContainer for the first time. */
    source?: string;
}
export interface BuildResult {
    manifest: ManifestFile;
    /** Library entries that were referenced. Useful for packing only the needed library subset. */
    referencedEntries: Set<string>;
    /** Nodes whose nodeType@version had no canonical library entry. */
    unresolvedTypes: string[];
}
export declare function buildManifest(overrides: FlowOverrides, library: Library, opts?: BuildOptions): BuildResult;
//# sourceMappingURL=manifest.d.ts.map