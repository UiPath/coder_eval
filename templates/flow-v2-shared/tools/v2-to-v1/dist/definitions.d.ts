/**
 * Synthesize the v1 `definitions[]` array for a flow.
 *
 * Each unique typeRef referenced by the manifest's nodes corresponds to
 * one definition entry. The library generator stashes a `<entry>.v1def.json`
 * sibling next to each canonical entry — that file already has the v1
 * shape (registry `Data.Node` minus `connectorMethodInfo` and
 * `outputResponseDefinition`, plus `supportsErrorHandling`/`inputDefaults`/
 * `debug`). We just load it.
 *
 * For typeRefs without a sidecar (custom nodes, missing library entries),
 * we omit the definition. The runtime will reject the flow if it depends
 * on a definition we can't produce — that's the correct failure mode and
 * shouldn't be silently masked.
 */
import type { Library } from 'v1-to-v2';
import type { NodeDefinition } from 'v1-to-v2';
export interface BuildDefinitionsOptions {
    library: Library;
    /** Library root on disk; needed to read `.v1def.json` sidecars. */
    libraryDir: string;
}
export interface BuildDefinitionsResult {
    definitions: NodeDefinition[];
    /** typeRefs we couldn't resolve to a sidecar. */
    missing: string[];
}
export declare function buildDefinitions(typeRefs: Set<string>, opts: BuildDefinitionsOptions, embedded?: Record<string, unknown>): BuildDefinitionsResult;
/**
 * The stable per-(connector, action) activity id lives in the connector's
 * v1def sidecar form, at
 *   form.sections[].fields[].componentProps.connectorDetail.uiPathActivityTypeId
 *
 * `uip` >= 1.2.0 (the 2026-05 validation tightening) rejects a connector
 * activity node whose `inputs.detail.uiPathActivityTypeId` is missing —
 * "Studio Web crashes when opening this flow". The v1→v2 distillation drops
 * the field as library-determined (see v1-to-v2 `LIBRARY_DETERMINED_FIELDS`),
 * so the v2→v1 rebuild has to re-populate it from the canonical sidecar.
 *
 * Returns undefined when the sidecar is absent or predates the field (older
 * fixtures, custom nodes) — the missing-definition path already surfaces that.
 */
export declare function loadActivityTypeId(libraryDir: string | undefined, nodeType: string, version: string): string | undefined;
//# sourceMappingURL=definitions.d.ts.map