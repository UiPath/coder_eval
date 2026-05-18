/**
 * Bake `ManifestFile` per-node data into the FIL's `action` and `trigger`
 * declaration bodies, so the emitted FIL is self-contained.
 *
 * Called from `convertV1ToV2` after `flowToFil` and `buildManifest` have run.
 * The flow-to-fil emitter already produced minimal action declarations (one
 * per executeNode-driven v1 node, body holding just an `id` override when
 * the identifier was sanitised). This pass:
 *
 *   - Adds `label`, `binding`, `folderBinding`, `rawInputs`, `inputs`,
 *     `configuration`, `configurationExtras`, `outputs`, `resource`,
 *     `resourceBindings`, and `fixture` to each action's body from the
 *     matching manifest entry.
 *   - Synthesises a `TriggerDeclaration` for every manifest entry whose
 *     type is `core.trigger.*`.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { ManifestFile } from './manifest';
export declare function populateProgramFromManifest(program: AST.Program, manifest: ManifestFile, 
/**
 * Ids of nodes that live at the top level of the v1 flow (i.e. in
 * `flow.nodes`, not inside `flow.subflows[*].nodes`). Trigger entries
 * outside this set are subflow-internal and don't become top-level
 * `trigger` declarations on the program. Pass an empty/undefined set to
 * accept every trigger entry (useful for non-flow callers that build a
 * manifest standalone).
 */
topLevelNodeIds?: Set<string>): void;
//# sourceMappingURL=manifest-to-fil.d.ts.map