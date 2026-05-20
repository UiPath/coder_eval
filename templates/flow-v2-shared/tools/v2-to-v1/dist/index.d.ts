/**
 * Public API for converting a Flow v2 project (FIL + bindings) back to a
 * Flow v1 .flow file.
 *
 * The hard part — turning sequential FIL `await` calls into a node graph
 * with edges, variables, and subflows — lives in `./fil-to-flow`. v2-to-v1
 * derives the in-memory manifest from the FIL's `flow`/`action`/`trigger`
 * declarations, then hands the per-node overrides + bindings to filToFlow.
 */
import type { FlowFile } from 'v1-to-v2';
import { Library, FieldsContainerCache } from 'v1-to-v2';
import type { ManifestFile, BindingsFile } from 'v1-to-v2';
import { ApplyResult } from './apply-input-expressions';
export { rehydrateConfiguration, RehydrateOptions, RehydrateResult, } from './configuration';
export { expandManifest } from './manifest';
export { buildDefinitions } from './definitions';
export { tryEmitV1Expression, Scope as ExpressionScope, EmitOptions as ExpressionEmitOptions, EmitResult as ExpressionEmitResult, } from './expression';
export { applyFilInputExpressions, ApplyResult as ApplyInputExpressionsResult, } from './apply-input-expressions';
export { filProgramToManifest } from './fil-to-manifest';
export { filToFlow, filToFlowWithScope, Scope, normalizeTypeVersions } from './fil-to-flow';
export interface ConvertOptions {
    /** Override the canonical library (default: ../integrations/library). */
    library?: Library;
    /** Override the library directory on disk (needed to load .v1def sidecars). */
    libraryDir?: string;
    /** Override the fieldsContainer cache (default: ../integrations/cache). */
    fieldsCache?: FieldsContainerCache | null;
}
export interface ConvertResult {
    flow: FlowFile;
    /** typeRefs whose canonical library entry was missing. */
    missingLibraryEntries: string[];
    /** typeRefs whose .v1def.json sidecar was missing. */
    missingDefinitions: string[];
    /** Phase A wire-up diagnostics (FIL executeNode args → inputs.detail). */
    inputExpressions: ApplyResult;
}
export declare function convertV2ToV1(filSource: string, manifest: ManifestFile | null | undefined, bindings: BindingsFile, opts?: ConvertOptions): ConvertResult;
//# sourceMappingURL=index.d.ts.map