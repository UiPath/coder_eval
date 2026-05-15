"use strict";
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
Object.defineProperty(exports, "__esModule", { value: true });
exports.isControlFlowNode = exports.isTriggerNode = exports.NODE_TYPES = exports.buildSubflowGraph = exports.buildGraph = exports.FlowGraph = exports.INLINED_NODE_TYPES = exports.SCRIPT_FN_PREFIX = exports.flowToFilWithDef = exports.flowToFil = exports.emitType = exports.emitExpression = exports.emitProgram = exports.envelopeConfiguration = exports.flattenConfiguration = exports.ESSENTIAL_KEYS = exports.distillConfiguration = exports.TIER_A_DEFAULTS = exports.loadDefaultFieldsCache = exports.FileSystemFieldsCache = exports.loadDefaultLibrary = exports.Library = void 0;
exports.convertV1ToV2 = convertV1ToV2;
const flow_to_fil_1 = require("./flow-to-fil");
const fil_emitter_1 = require("./fil-emitter");
const library_1 = require("./library");
const manifest_1 = require("./manifest");
const bindings_1 = require("./bindings");
const manifest_to_fil_1 = require("./manifest-to-fil");
const configuration_1 = require("./configuration");
var library_2 = require("./library");
Object.defineProperty(exports, "Library", { enumerable: true, get: function () { return library_2.Library; } });
Object.defineProperty(exports, "loadDefaultLibrary", { enumerable: true, get: function () { return library_2.loadDefaultLibrary; } });
var configuration_2 = require("./configuration");
Object.defineProperty(exports, "FileSystemFieldsCache", { enumerable: true, get: function () { return configuration_2.FileSystemFieldsCache; } });
Object.defineProperty(exports, "loadDefaultFieldsCache", { enumerable: true, get: function () { return configuration_2.loadDefaultFieldsCache; } });
Object.defineProperty(exports, "TIER_A_DEFAULTS", { enumerable: true, get: function () { return configuration_2.TIER_A_DEFAULTS; } });
Object.defineProperty(exports, "distillConfiguration", { enumerable: true, get: function () { return configuration_2.distillConfiguration; } });
var envelope_1 = require("./envelope");
Object.defineProperty(exports, "ESSENTIAL_KEYS", { enumerable: true, get: function () { return envelope_1.ESSENTIAL_KEYS; } });
Object.defineProperty(exports, "flattenConfiguration", { enumerable: true, get: function () { return envelope_1.flattenConfiguration; } });
Object.defineProperty(exports, "envelopeConfiguration", { enumerable: true, get: function () { return envelope_1.envelopeConfiguration; } });
// ─── Re-exports for downstream packages (v2-to-v1, cs2fil) ───────────────────
//
// These were previously in `flow2fil`. v1-to-v2 now owns this layer; everyone
// imports from here.
var fil_emitter_2 = require("./fil-emitter");
// FIL emission
Object.defineProperty(exports, "emitProgram", { enumerable: true, get: function () { return fil_emitter_2.emitProgram; } });
Object.defineProperty(exports, "emitExpression", { enumerable: true, get: function () { return fil_emitter_2.emitExpression; } });
Object.defineProperty(exports, "emitType", { enumerable: true, get: function () { return fil_emitter_2.emitType; } });
var flow_to_fil_2 = require("./flow-to-fil");
// flow → FIL graph traversal
Object.defineProperty(exports, "flowToFil", { enumerable: true, get: function () { return flow_to_fil_2.flowToFil; } });
Object.defineProperty(exports, "flowToFilWithDef", { enumerable: true, get: function () { return flow_to_fil_2.flowToFilWithDef; } });
Object.defineProperty(exports, "SCRIPT_FN_PREFIX", { enumerable: true, get: function () { return flow_to_fil_2.SCRIPT_FN_PREFIX; } });
Object.defineProperty(exports, "INLINED_NODE_TYPES", { enumerable: true, get: function () { return flow_to_fil_2.INLINED_NODE_TYPES; } });
var flow_graph_1 = require("./flow-graph");
// graph utilities
Object.defineProperty(exports, "FlowGraph", { enumerable: true, get: function () { return flow_graph_1.FlowGraph; } });
Object.defineProperty(exports, "buildGraph", { enumerable: true, get: function () { return flow_graph_1.buildGraph; } });
Object.defineProperty(exports, "buildSubflowGraph", { enumerable: true, get: function () { return flow_graph_1.buildSubflowGraph; } });
var flow_types_1 = require("./flow-types");
Object.defineProperty(exports, "NODE_TYPES", { enumerable: true, get: function () { return flow_types_1.NODE_TYPES; } });
Object.defineProperty(exports, "isTriggerNode", { enumerable: true, get: function () { return flow_types_1.isTriggerNode; } });
Object.defineProperty(exports, "isControlFlowNode", { enumerable: true, get: function () { return flow_types_1.isControlFlowNode; } });
function convertV1ToV2(flow, opts = {}) {
    const library = opts.library ?? (0, library_1.loadDefaultLibrary)();
    // `null` opts.fieldsCache disables caching; `undefined` uses the default.
    const cache = opts.fieldsCache === null
        ? undefined
        : (opts.fieldsCache ?? (0, configuration_1.loadDefaultFieldsCache)());
    const source = opts.source ?? flow.id ?? flow.name ?? 'unknown';
    const { program, defFlow } = (0, flow_to_fil_1.flowToFilWithDef)(flow);
    // flow-to-fil only collects overrides for nodes it actually walks; in flows
    // with multi-branch fan-outs, some nodes get skipped. Backfill from
    // flow.nodes directly so the v2 manifest preserves every v1 node id.
    for (const n of flow.nodes ?? []) {
        if (defFlow.nodeOverrides[n.id])
            continue;
        defFlow.nodeOverrides[n.id] = nodeToOverride(n);
    }
    const { manifest, referencedEntries, unresolvedTypes } = (0, manifest_1.buildManifest)(defFlow, library, { fieldsCache: cache, source });
    const bindings = (0, bindings_1.buildBindings)(flow.bindings ?? []);
    // Phase 3: bake every manifest entry's per-instance data into the FIL's
    // action / trigger declaration bodies. This makes the FIL self-contained
    // so the manifest.flow sidecar can be dropped — `v2-to-v1` (Phase 2)
    // already knows how to derive the equivalent manifest from FIL alone.
    // `topLevelNodeIds` keeps subflow-internal trigger nodes from polluting
    // the main program's top-level `trigger` declarations.
    const topLevelNodeIds = new Set((flow.nodes ?? []).map((n) => n.id));
    (0, manifest_to_fil_1.populateProgramFromManifest)(program, manifest, topLevelNodeIds);
    // Emit FIL *after* populating action bodies so the rendered source carries
    // the bindings, rawInputs, labels, etc. inline.
    const fil = (0, fil_emitter_1.emitProgram)(program);
    // Embed v1 definitions[] for any typeRef the canonical library doesn't
    // cover — system types not yet added to the library, and tenant-specific
    // types (uipath.core.agent.<uuid>, etc.) that can never be in a global
    // library. v2→v1 reads from here as a fallback.
    const referencedTypeRefs = new Set();
    for (const n of Object.values(manifest.nodes))
        referencedTypeRefs.add(n.type);
    const embedded = {};
    for (const def of flow.definitions ?? []) {
        const typeRef = `${def.nodeType}@${def.version}`;
        if (!referencedTypeRefs.has(typeRef))
            continue;
        if (library.has(def.nodeType, def.version))
            continue;
        embedded[typeRef] = def;
    }
    if (Object.keys(embedded).length > 0) {
        manifest.embeddedDefinitions = embedded;
    }
    return {
        fil,
        manifest,
        bindings,
        referencedEntries: [...referencedEntries].sort(),
        unresolvedTypes,
    };
}
/** Convert a raw v1 NodeInstance into the override shape flow-to-fil produces. */
function nodeToOverride(n) {
    const o = {
        type: n.type,
        typeVersion: n.typeVersion,
    };
    if (n.display)
        o.display = n.display;
    if (n.ui)
        o.ui = n.ui;
    if (n.model)
        o.model = n.model;
    if (n.inputs)
        o.inputs = n.inputs;
    if (n.outputs)
        o.outputs = n.outputs;
    return o;
}
//# sourceMappingURL=index.js.map