"use strict";
/**
 * Public API for converting a Flow v2 project (FIL + bindings) back to a
 * Flow v1 .flow file.
 *
 * The hard part — turning sequential FIL `await` calls into a node graph
 * with edges, variables, and subflows — lives in `./fil-to-flow`. v2-to-v1
 * derives the in-memory manifest from the FIL's `flow`/`action`/`trigger`
 * declarations, then hands the per-node overrides + bindings to filToFlow.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.filToFlowWithScope = exports.filToFlow = exports.filProgramToManifest = exports.applyFilInputExpressions = exports.tryEmitV1Expression = exports.buildDefinitions = exports.expandManifest = exports.rehydrateConfiguration = void 0;
exports.convertV2ToV1 = convertV2ToV1;
const path = __importStar(require("path"));
const fs = __importStar(require("fs"));
const parser_1 = require("fil-compiler/dist/parser");
const fil_to_flow_1 = require("./fil-to-flow");
const v1_to_v2_1 = require("v1-to-v2");
const manifest_1 = require("./manifest");
const definitions_1 = require("./definitions");
const apply_input_expressions_1 = require("./apply-input-expressions");
const fil_to_manifest_1 = require("./fil-to-manifest");
var configuration_1 = require("./configuration");
Object.defineProperty(exports, "rehydrateConfiguration", { enumerable: true, get: function () { return configuration_1.rehydrateConfiguration; } });
var manifest_2 = require("./manifest");
Object.defineProperty(exports, "expandManifest", { enumerable: true, get: function () { return manifest_2.expandManifest; } });
var definitions_2 = require("./definitions");
Object.defineProperty(exports, "buildDefinitions", { enumerable: true, get: function () { return definitions_2.buildDefinitions; } });
var expression_1 = require("./expression");
Object.defineProperty(exports, "tryEmitV1Expression", { enumerable: true, get: function () { return expression_1.tryEmitV1Expression; } });
var apply_input_expressions_2 = require("./apply-input-expressions");
Object.defineProperty(exports, "applyFilInputExpressions", { enumerable: true, get: function () { return apply_input_expressions_2.applyFilInputExpressions; } });
var fil_to_manifest_2 = require("./fil-to-manifest");
Object.defineProperty(exports, "filProgramToManifest", { enumerable: true, get: function () { return fil_to_manifest_2.filProgramToManifest; } });
// ─── FIL → Flow graph reconstruction (re-exports for cs2fil) ─────────────────
var fil_to_flow_2 = require("./fil-to-flow");
Object.defineProperty(exports, "filToFlow", { enumerable: true, get: function () { return fil_to_flow_2.filToFlow; } });
Object.defineProperty(exports, "filToFlowWithScope", { enumerable: true, get: function () { return fil_to_flow_2.filToFlowWithScope; } });
function convertV2ToV1(filSource, manifest, bindings, opts = {}) {
    const library = opts.library ?? (0, v1_to_v2_1.loadDefaultLibrary)();
    const libraryDir = opts.libraryDir ?? (0, v1_to_v2_1.defaultLibraryDir)();
    const cache = opts.fieldsCache === null
        ? undefined
        : (opts.fieldsCache ?? (0, v1_to_v2_1.loadDefaultFieldsCache)());
    // 1. Parse FIL → AST.
    const parser = new parser_1.Parser(filSource);
    const program = parser.parse();
    // 1b. If no manifest was supplied, synthesize one from the FIL's
    //     top-level `flow`/`action`/`trigger` declarations. The declarations
    //     are the source of truth for per-node metadata; in-memory manifest
    //     objects are only used by callers that already have one resolved.
    if (!manifest) {
        const derived = (0, fil_to_manifest_1.filProgramToManifest)(program);
        if (!derived) {
            throw new Error('convertV2ToV1: the FIL has no `flow`/`action`/`trigger` ' +
                'declarations to derive a manifest from. Add the declarations to ' +
                'the FIL.');
        }
        manifest = derived;
    }
    // 2. Expand manifest entries → NodeOverrides for filToFlow. The manifest
    //    references bindings by symbolic ID; we need bindings.json so we can
    //    write the real UUIDs into v1's inputs.detail.connectionId.
    const expanded = (0, manifest_1.expandManifest)(manifest, { library, fieldsCache: cache, bindings });
    // 3. Synthesize definitions[] from embedded fallbacks + canonical-library v1def sidecars.
    //    Include manifest-declared types, every key the user listed in
    //    embeddedDefinitions, AND every core type bundled by this package.
    //    The bundled core defs cover trigger/end/decision/merge/script/etc. —
    //    types the converter auto-generates from FIL syntax. Without seeding
    //    these into overrides, filToFlow's patchEdgePorts can't repair edges
    //    to those auto-generated nodes (the script node uses `success` not
    //    `output` as its source handle, etc.).
    const bundledCoreDefs = loadBundledCoreDefinitions();
    const synthesizedProcessResourceDefs = buildProcessResourceDefinitionsFromManifest(manifest);
    const userEmbedded = manifest.embeddedDefinitions ?? {};
    const allEmbedded = {
        ...bundledCoreDefs,
        ...synthesizedProcessResourceDefs,
        ...userEmbedded,
    };
    const seedTypeRefs = new Set(expanded.typeRefs);
    for (const k of Object.keys(allEmbedded))
        seedTypeRefs.add(k);
    const builtDefs = (0, definitions_1.buildDefinitions)(seedTypeRefs, { library, libraryDir }, allEmbedded);
    // 4. Build a FlowOverrides bundle and hand to filToFlow.
    const overrides = {
        flowId: manifest.flowId,
        flowName: manifest.flowName,
        flowVersion: manifest.flowVersion,
        nodeOverrides: expanded.overrides,
        definitions: builtDefs.definitions,
        bindings: bindings.bindings,
    };
    if (manifest.solutionId)
        overrides.solutionId = manifest.solutionId;
    if (manifest.projectId)
        overrides.projectId = manifest.projectId;
    if (manifest.variables)
        overrides.variableOverrides = manifest.variables;
    // 5. Reconstruct nodes/edges/variables from FIL.
    const flow = (0, fil_to_flow_1.filToFlow)(program, manifest.flowId, overrides);
    // 5b. filToFlow synthesizes additional nodes (decisions, merges, setvar
    //     scripts, end nodes, …) from FIL control flow. Their typeRefs aren't
    //     in `expanded.typeRefs` (which only covers manifest-declared nodes),
    //     so we collect them from the resulting flow and rebuild definitions
    //     to cover everything actually present. Without this, `uip maestro
    //     flow validate` rejects the .flow with a missing-definition error.
    const allTypeRefs = collectAllTypeRefs(flow);
    for (const t of expanded.typeRefs)
        allTypeRefs.add(t);
    const finalDefs = (0, definitions_1.buildDefinitions)(allTypeRefs, { library, libraryDir }, allEmbedded);
    flow.definitions = finalDefs.definitions;
    // 6. Phase A wire-up: fold each `executeNode("nodeId", {…})` argument
    //    into the corresponding integration node's
    //    `inputs.detail.configuration`. FIL is the source of truth for
    //    per-instance field values when the agent authors flows directly.
    const inputExpressions = (0, apply_input_expressions_1.applyFilInputExpressions)(flow, program);
    return {
        flow,
        missingLibraryEntries: expanded.missingLibraryEntries,
        missingDefinitions: finalDefs.missing,
        inputExpressions,
    };
}
/**
 * Lazily load the bundled `core-definitions.json` — a snapshot of v1's
 * canonical definitions for trigger/end/decision/merge/script/etc. taken
 * from a real .flow file. Used as a fallback when the manifest doesn't
 * carry embeddedDefinitions for these types (the common case for
 * agent-authored v2 flows).
 */
let bundledCoreDefsCache;
function loadBundledCoreDefinitions() {
    if (bundledCoreDefsCache)
        return bundledCoreDefsCache;
    // After tsc, dist/index.js is the entry; src/core-definitions.json sits at
    // ../src/. For ts-node/tests, __dirname is src/ so it resolves directly.
    const candidates = [
        path.resolve(__dirname, '..', 'src', 'core-definitions.json'),
        path.resolve(__dirname, 'core-definitions.json'),
    ];
    for (const p of candidates) {
        if (fs.existsSync(p)) {
            try {
                bundledCoreDefsCache = JSON.parse(fs.readFileSync(p, 'utf8'));
                return bundledCoreDefsCache;
            }
            catch {
                // fall through to next candidate
            }
        }
    }
    bundledCoreDefsCache = {};
    return bundledCoreDefsCache;
}
function buildProcessResourceDefinitionsFromManifest(manifest) {
    const out = {};
    for (const node of Object.values(manifest.nodes)) {
        const [nodeType, version = '1.0.0'] = node.type.split('@');
        const spec = processResourceDefinitionSpec(nodeType);
        if (spec) {
            out[node.type] = buildProcessResourceDefinition(nodeType, version, node, spec);
            continue;
        }
        if (nodeType === 'uipath.agent.autonomous') {
            out[node.type] = buildInlineAgentDefinition(version, node);
        }
    }
    return out;
}
const PROCESS_RESOURCE_DEFINITION_SPECS = [
    {
        prefix: 'uipath.core.agent.',
        category: 'agent',
        icon: 'autonomous-agent',
        sortOrder: 530,
        resourceSubType: 'Agent',
        orchestratorType: 'agent',
        serviceType: 'Orchestrator.StartAgentJob',
        defaultLabel: 'Agent',
    },
    {
        prefix: 'uipath.core.api-workflow.',
        category: 'api-workflow',
        icon: 'api',
        sortOrder: 510,
        resourceSubType: 'Api',
        orchestratorType: 'api',
        serviceType: 'Orchestrator.ExecuteApiWorkflowAsync',
        defaultLabel: 'API Workflow',
        supportsErrorHandling: true,
    },
    {
        prefix: 'uipath.core.rpa-workflow.',
        category: 'rpa-workflow',
        icon: 'rpa',
        sortOrder: 530,
        resourceSubType: 'Process',
        orchestratorType: 'process',
        serviceType: 'Orchestrator.StartJob',
        defaultLabel: 'RPA Workflow',
        supportsErrorHandling: true,
        inputDefaultsFromRawInputs: true,
    },
];
function processResourceDefinitionSpec(nodeType) {
    return PROCESS_RESOURCE_DEFINITION_SPECS.find((spec) => nodeType.startsWith(spec.prefix));
}
function buildProcessResourceDefinition(nodeType, version, node, spec) {
    const label = node.label ?? spec.defaultLabel;
    const resource = node.resource ?? { resource: 'process', resourceSubType: spec.resourceSubType };
    return {
        nodeType,
        version,
        category: spec.category,
        tags: [],
        sortOrder: spec.sortOrder,
        ...(spec.supportsErrorHandling ? { supportsErrorHandling: true } : {}),
        display: {
            label,
            icon: spec.icon,
        },
        handleConfiguration: [
            {
                position: 'left',
                handles: [
                    {
                        id: 'input',
                        type: 'target',
                        handleType: 'input',
                        label: 'Input',
                    },
                ],
            },
            {
                position: 'right',
                handles: [
                    {
                        id: 'output',
                        type: 'source',
                        handleType: 'output',
                        label: 'Output',
                    },
                    {
                        id: 'error',
                        type: 'source',
                        handleType: 'output',
                        label: 'Error',
                        visible: '{inputs.errorHandlingEnabled}',
                        constraints: {
                            maxConnections: 1,
                        },
                    },
                ],
            },
        ],
        model: {
            type: 'bpmn:ServiceTask',
            serviceType: resource.serviceType ?? spec.serviceType,
            version: 'v2',
            ...(resource.section ? { section: resource.section } : {}),
            debug: {
                runtime: 'bpmnEngine',
            },
            bindings: {
                resource: resource.resource ?? 'process',
                resourceSubType: resource.resourceSubType ?? spec.resourceSubType,
                ...(resource.resourceKey ? { resourceKey: resource.resourceKey } : {}),
                orchestratorType: resource.orchestratorType ?? spec.orchestratorType,
                values: {
                    name: label,
                    folderPath: '',
                },
            },
            context: [
                {
                    name: 'name',
                    type: 'string',
                    value: '<bindings.name>',
                },
                {
                    name: 'folderPath',
                    type: 'string',
                    value: '<bindings.folderPath>',
                },
                {
                    name: '_label',
                    type: 'string',
                    value: label,
                },
            ],
        },
        ...(node.rawInputs && Object.keys(node.rawInputs).length > 0
            ? {
                inputDefinition: inferInputDefinition(node.rawInputs),
                inputDefaults: spec.inputDefaultsFromRawInputs ? { ...node.rawInputs } : {},
            }
            : {}),
        outputDefinition: node.outputs && Object.keys(node.outputs).length > 0
            ? node.outputs
            : defaultProcessResourceOutputDefinition(spec),
    };
}
function inferInputDefinition(rawInputs) {
    const properties = {};
    for (const [key, value] of Object.entries(rawInputs ?? {})) {
        if (key === 'fixture')
            continue;
        properties[key] = { type: jsonSchemaType(value) };
    }
    return { type: 'object', properties };
}
function jsonSchemaType(value) {
    if (Array.isArray(value))
        return 'array';
    if (value === null)
        return 'null';
    return typeof value === 'number'
        ? 'number'
        : typeof value === 'boolean'
            ? 'boolean'
            : typeof value === 'object'
                ? 'object'
                : 'string';
}
function defaultProcessResourceOutputDefinition(spec) {
    return spec.resourceSubType === 'Agent'
        ? {
            output: {
                type: 'object',
                description: 'The return value of the agent',
                source: '=result.response',
                var: 'output',
            },
            error: {
                type: 'object',
                description: 'Error information if the agent fails',
                source: '=result.Error',
                var: 'error',
            },
        }
        : {
            error: {
                type: 'object',
                description: 'Error information if the node fails',
                source: '=Error',
                var: 'error',
            },
        };
}
function buildInlineAgentDefinition(version, node) {
    const rawInputs = node.rawInputs ?? {};
    return {
        nodeType: 'uipath.agent.autonomous',
        version,
        category: 'agent',
        tags: ['agentic', 'ai', 'autonomous', 'agent'],
        sortOrder: 530,
        display: {
            label: 'Autonomous Agent',
            icon: 'autonomous-agent',
            shape: 'rectangle',
        },
        handleConfiguration: [
            {
                position: 'top',
                handles: [
                    {
                        id: 'escalation',
                        type: 'source',
                        handleType: 'artifact',
                        label: 'Escalations',
                        showButton: true,
                        constraints: {
                            allowedTargets: [{ nodeType: 'uipath.agent.resource.escalation' }],
                            validationMessage: 'Only Agent Escalation nodes can connect here',
                        },
                    },
                ],
            },
            {
                position: 'bottom',
                handles: [
                    {
                        id: 'context',
                        type: 'source',
                        handleType: 'artifact',
                        label: 'Context',
                        showButton: true,
                        constraints: {
                            allowedTargets: [{ nodeType: 'uipath.agent.resource.context.*' }],
                            validationMessage: 'Only Context resource nodes can connect here',
                        },
                    },
                    {
                        id: 'tool',
                        type: 'source',
                        handleType: 'artifact',
                        label: 'Tools',
                        showButton: true,
                        constraints: {
                            allowedTargets: [{ nodeType: 'uipath.agent.resource.tool.*' }],
                            validationMessage: 'Only Tool resource nodes can connect here',
                        },
                    },
                ],
            },
            {
                position: 'left',
                handles: [
                    {
                        id: 'input',
                        type: 'target',
                        handleType: 'input',
                    },
                ],
            },
            {
                position: 'right',
                handles: [
                    {
                        id: 'success',
                        type: 'source',
                        handleType: 'output',
                    },
                    {
                        id: 'error',
                        type: 'source',
                        handleType: 'output',
                        label: 'Error',
                        visible: '{inputs.errorHandlingEnabled}',
                        constraints: {
                            maxConnections: 1,
                        },
                    },
                ],
            },
        ],
        inputDefinition: {
            type: 'object',
            properties: {
                systemPrompt: {
                    type: 'string',
                    minLength: 1,
                    errorMessage: { minLength: 'System prompt is required' },
                },
                userPrompt: {
                    type: 'string',
                    minLength: 1,
                    errorMessage: { minLength: 'User prompt is required' },
                },
                model: {
                    type: 'string',
                    minLength: 1,
                    errorMessage: { minLength: 'Model is required' },
                },
                temperature: { type: 'number', minimum: 0, maximum: 1 },
                maxTokenPerResponse: { type: 'number', minimum: 0, maximum: 16384 },
                maxIterations: { type: 'number', minimum: 1, maximum: 100 },
                guardrails: { type: 'array' },
                agentInputVariables: { type: 'array' },
                agentOutputVariables: { type: 'array' },
            },
            required: ['systemPrompt', 'userPrompt', 'model'],
        },
        inputDefaults: {
            systemPrompt: typeof rawInputs.systemPrompt === 'string'
                ? rawInputs.systemPrompt
                : 'You are an agentic assistant.',
            userPrompt: typeof rawInputs.userPrompt === 'string'
                ? rawInputs.userPrompt
                : 'What is the current date?',
            model: typeof rawInputs.model === 'string'
                ? rawInputs.model
                : 'gpt-4o-2024-11-20',
            agentInputVariables: Array.isArray(rawInputs.agentInputVariables)
                ? rawInputs.agentInputVariables
                : [],
            agentOutputVariables: Array.isArray(rawInputs.agentOutputVariables)
                ? rawInputs.agentOutputVariables
                : [{ id: 'content', type: 'string' }],
        },
        outputDefinition: node.outputs && Object.keys(node.outputs).length > 0
            ? node.outputs
            : {
                output: {
                    type: 'object',
                    description: 'Agent response',
                    source: '=result.response',
                    var: 'output',
                    properties: {
                        content: {
                            type: 'string',
                            description: 'The agent response text',
                        },
                    },
                },
                error: {
                    type: 'object',
                    description: 'Error information if the node fails',
                    source: '=Error',
                    var: 'error',
                },
            },
        model: {
            source: true,
            type: 'bpmn:ServiceTask',
            serviceType: 'Orchestrator.StartInlineAgentJob',
            version: 'v2',
            context: [
                { name: '_label', type: 'string', value: node.label ?? '' },
                { name: 'name', type: 'string', value: '' },
                { name: 'entryPoint', type: 'string', value: '' },
            ],
        },
    };
}
/**
 * Walk the flow's nodes (and subflow nodes) collecting every `<type>@<version>`
 * referenced. Used to ensure `definitions[]` covers every node the converter
 * actually emitted, not just the manifest-declared ones.
 */
function collectAllTypeRefs(flow) {
    const out = new Set();
    const add = (type, version) => out.add(`${type}@${version ?? '1.0.0'}`);
    for (const n of flow.nodes)
        add(n.type, n.typeVersion);
    for (const sf of Object.values(flow.subflows ?? {})) {
        for (const n of sf.nodes)
            add(n.type, n.typeVersion);
    }
    return out;
}
//# sourceMappingURL=index.js.map