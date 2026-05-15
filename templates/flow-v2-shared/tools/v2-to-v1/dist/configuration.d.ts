/**
 * Re-hydrate the v1 `inputs.detail.configuration` JSON-string from a v2
 * manifest node. Inverse of `distillConfiguration` in v1-to-v2.
 *
 * Composition order (later wins):
 *   1. Tier A defaults — globally constant fields the editor stamps on
 *      every connector node (additionalHeaders, language, etc.).
 *   2. Tier B values — synthesized from the canonical library entry
 *      (connectorKey, objectName, httpMethod, path, instanceParameters,
 *       connectorName, objectDisplayName).
 *   3. fieldsContainer — restored from a sidecar cache when one exists
 *      for the (connector, action, version) typeRef.
 *   4. node.configuration — fields the v1→v2 distillation kept verbatim
 *      because they don't fit any tier rule (connectorVersion, _textBlocks,
 *      unifiedTypesCompatible, etc.).
 *   5. node.configurationExtras — fields whose v1 values diverged from
 *      what the library or defaults predicted; preserved for round-trip.
 */
import { LibraryEntry, FieldsContainerCache } from 'v1-to-v2';
export interface RehydrateOptions {
    /** Sidecar cache for `fieldsContainer` blobs. Optional. */
    fieldsCache?: FieldsContainerCache;
    /** typeRef (`<nodeType>@<version>`) for cache lookup. */
    typeRef: string;
    /** Canonical library entry. Required. */
    library: LibraryEntry;
    /** v2 node's residual configuration (fields the distill pass kept). */
    configuration?: Record<string, unknown>;
    /** v2 node's extras (fields that diverged from defaults/library). */
    extras?: Record<string, unknown>;
}
export interface RehydrateResult {
    /** The composed `inputs.detail.configuration` object. */
    config: Record<string, unknown>;
    /** Same as a `=jsonString:{...}` expression — what v1 stores. */
    configString: string;
    /** Whether a sidecar fieldsContainer was applied. */
    fieldsContainerRestored: boolean;
}
export declare function rehydrateConfiguration(opts: RehydrateOptions): RehydrateResult;
//# sourceMappingURL=configuration.d.ts.map