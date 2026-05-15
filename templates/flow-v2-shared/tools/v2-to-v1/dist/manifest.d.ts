/**
 * Expand a v2 manifest node back into a flow2fil `NodeOverride` — the
 * shape `flow2fil.filToFlow` consumes when reconstructing the full v1
 * .flow file from FIL source.
 *
 * For integration nodes (`uipath.connector.*`):
 *   - Look up the canonical library entry.
 *   - Rebuild `inputs.detail` from library + bindings + residual inputs +
 *     rehydrated configuration string.
 *   - Restore `display` from the library, with the v2 `label` override
 *     applied on top.
 *   - Pass through `outputs` and `ui` verbatim.
 *
 * For non-integration nodes (control flow, agents, scripts, …) the v2
 * manifest carries `rawInputs` — the v1 `inputs` blob captured verbatim.
 * We just put it back where it came from.
 */
import { Library, FieldsContainerCache, BindingsFile } from 'v1-to-v2';
import type { ManifestFile } from 'v1-to-v2/dist/manifest';
import type { NodeOverride } from 'v1-to-v2';
export interface ExpandOptions {
    library: Library;
    fieldsCache?: FieldsContainerCache;
    /**
     * Required for integration nodes — the manifest references bindings by
     * symbolic ID (e.g. "bOutlook"); we resolve back to the real UUID by
     * looking up each binding entry's `resourceKey`/`default`.
     */
    bindings?: BindingsFile;
}
export interface ExpandResult {
    overrides: Record<string, NodeOverride>;
    /** Set of typeRefs (`<nodeType>@<version>`) actually referenced. */
    typeRefs: Set<string>;
    /** typeRefs whose canonical library entry couldn't be found. */
    missingLibraryEntries: string[];
}
export declare function expandManifest(manifest: ManifestFile, opts: ExpandOptions): ExpandResult;
//# sourceMappingURL=manifest.d.ts.map