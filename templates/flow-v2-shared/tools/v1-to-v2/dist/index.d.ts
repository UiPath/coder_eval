/**
 * Public API for converting a Flow v1 file (.flow JSON) to the Flow v2 form:
 *
 *   v2 = FIL code (.fil) + bindings (bindings.json)
 *
 * Owns the v1 .flow → FIL AST conversion machinery (flow-to-fil, flow-graph,
 * fil-emitter, flow-types) so v2-to-v1 and cs2fil don't need flow2fil. The
 * conversion still builds an in-memory manifest as an intermediate, then
 * bakes that per-node metadata into FIL action/trigger declarations.
 */
import { FlowFile } from './flow-types';
import { Library } from './library';
import { ManifestFile } from './manifest';
import { BindingsFile } from './bindings';
import { FieldsContainerCache } from './configuration';
export { Library, LibraryEntry, loadDefaultLibrary } from './library';
export { ManifestFile, ManifestNode } from './manifest';
export { BindingsFile, BindingEntry } from './bindings';
export { FieldsContainerCache, FileSystemFieldsCache, loadDefaultFieldsCache, TIER_A_DEFAULTS, distillConfiguration, } from './configuration';
export { ESSENTIAL_KEYS, flattenConfiguration, envelopeConfiguration, } from './envelope';
export { emitProgram, emitExpression, emitType, } from './fil-emitter';
export { flowToFil, flowToFilWithDef, SCRIPT_FN_PREFIX, INLINED_NODE_TYPES, } from './flow-to-fil';
export { FlowGraph, buildGraph, buildSubflowGraph, OutEdge, InEdge, } from './flow-graph';
export { FlowFile, FlowDefinitionFile, NodeInstance, NodeOverride, NodeDefinition, NodeDisplay, NodeUI, EdgeInstance, WorkflowVariables, GlobalVariable, NodeVariable, VariableUpdate, SubflowEntry, Metadata, NODE_TYPES, isTriggerNode, isControlFlowNode, } from './flow-types';
export interface ConvertResult {
    /** FIL TypeScript source code. */
    fil: string;
    /** In-memory manifest intermediate (per-node implementation entries, variables, ui). */
    manifest: ManifestFile;
    /** v1 bindings[] reflowed into a standalone document. */
    bindings: BindingsFile;
    /** Library entries referenced by this Flow (for packing the relevant subset). */
    referencedEntries: string[];
    /** Integration nodes whose nodeType@version had no canonical entry. */
    unresolvedTypes: string[];
}
export interface ConvertOptions {
    /** Override the library location (default: ../integrations/library). */
    library?: Library;
    /** Override the fieldsContainer cache (default: ../integrations/cache). */
    fieldsCache?: FieldsContainerCache | null;
    /** Identifier recorded in the cache when a new fieldsContainer is captured. */
    source?: string;
}
export declare function convertV1ToV2(flow: FlowFile, opts?: ConvertOptions): ConvertResult;
//# sourceMappingURL=index.d.ts.map