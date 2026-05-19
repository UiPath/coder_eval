"use strict";
// Loads FIL + bindings.json from a project directory and runs pre-flight
// validation: every connector node must have connection + folder bindings
// that resolve to real UUIDs and (optionally) match a real connection in
// `uip is connections list`. The connector library must contain a matching
// definition with a CRUD-shaped operation.
//
// The in-memory manifest the dispatcher and preflight resolver consume is
// derived from the FIL's `flow`/`action`/`trigger` declarations.
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
exports.loadProject = loadProject;
exports.preflight = preflight;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const parser_1 = require("fil-compiler/dist/parser");
const fil_to_manifest_1 = require("./fil-to-manifest");
const CRUD_VERBS = {
    Retrieve: 'get',
    Create: 'create',
    Update: 'update',
    Delete: 'delete',
    Replace: 'replace',
    List: 'list',
};
const ZERO_UUID_PREFIX = '00000000-0000-0000-0000-';
const AGENT_NODE_PREFIX = 'uipath.core.agent.';
const AGENT_SERVICE_TYPE = 'Orchestrator.StartAgentJob';
const API_WORKFLOW_NODE_PREFIX = 'uipath.core.api-workflow.';
const API_WORKFLOW_SERVICE_TYPE = 'Orchestrator.ExecuteApiWorkflowAsync';
const RPA_WORKFLOW_NODE_PREFIX = 'uipath.core.rpa-workflow.';
const RPA_WORKFLOW_SERVICE_TYPE = 'Orchestrator.StartJob';
const INLINE_AGENT_NODE_TYPE = 'uipath.agent.autonomous';
const INLINE_AGENT_SERVICE_TYPE = 'Orchestrator.StartInlineAgentJob';
const HITL_NODE_TYPE = 'uipath.human-in-the-loop';
const SUMMARIZE_NODE_TYPE = 'uipath.pattern.deep-rag';
// ─── Project discovery ───────────────────────────────────────────────────────
function loadProject(projectDir, opts = {}) {
    const filPath = opts.filPath ?? autodiscoverFil(projectDir);
    const filSource = fs.readFileSync(filPath, 'utf8');
    const program = new parser_1.Parser(filSource).parse();
    const manifest = (0, fil_to_manifest_1.filProgramToManifest)(program);
    const bindingsPath = opts.bindingsPath ?? path.join(projectDir, 'bindings.json');
    let bindings;
    if (fs.existsSync(bindingsPath)) {
        bindings = JSON.parse(fs.readFileSync(bindingsPath, 'utf8'));
    }
    return {
        filPath,
        bindingsPath: bindings ? bindingsPath : undefined,
        filSource,
        flowId: manifest.flowId,
        flowName: manifest.flowName,
        flowVersion: manifest.flowVersion,
        manifest,
        bindings,
    };
}
function autodiscoverFil(dir) {
    const all = fs.readdirSync(dir);
    const entries = all.filter(f => f.endsWith('.fil'));
    if (entries.length === 0) {
        const legacy = all.filter(f => f.endsWith('.fil.ts'));
        if (legacy.length > 0) {
            const sample = legacy[0];
            const renamed = sample.replace(/\.fil\.ts$/, '.fil');
            throw new Error(`found legacy FIL source "${sample}" in ${dir}. ` +
                `Flow v2 now uses .fil (no .ts suffix). ` +
                `Rename: mv ${path.join(dir, sample)} ${path.join(dir, renamed)}`);
        }
        throw new Error(`no .fil file in ${dir}`);
    }
    if (entries.length > 1) {
        throw new Error(`ambiguous .fil match in ${dir}: ${entries.join(', ')}. Pass --fil explicitly.`);
    }
    return path.join(dir, entries[0]);
}
// ─── Pre-flight validation ───────────────────────────────────────────────────
function preflight(project, libraryDir, connections) {
    const errors = [];
    const warnings = [];
    const nodes = [];
    const bindingResolver = buildBindingResolver(project.bindings);
    resolveAllNodes(project, libraryDir, bindingResolver, nodes, errors, warnings);
    // Connection verification only applies to connector nodes.
    const connectorNodes = nodes.filter((n) => n.kind === 'connector');
    if (connections !== null && connectorNodes.length > 0) {
        verifyConnections(connectorNodes, connections, errors, warnings);
    }
    return { nodes, warnings, errors };
}
function buildBindingResolver(bindings) {
    const byId = new Map();
    for (const b of bindings?.bindings ?? []) {
        if (b.id)
            byId.set(b.id, b);
    }
    return {
        resolve(symbolicId) {
            const entry = byId.get(symbolicId);
            if (!entry)
                return { kind: 'missing' };
            const raw = (typeof entry.resourceKey === 'string' && entry.resourceKey)
                || (typeof entry.default === 'string' && entry.default)
                || '';
            if (!raw || isStubUuid(raw))
                return { kind: 'stub', raw };
            return { kind: 'ok', uuid: raw };
        },
        get(symbolicId) { return byId.get(symbolicId); },
        knownIds() { return [...byId.keys()]; },
    };
}
function resolveAllNodes(project, libraryDir, bindingResolver, out, errors, warnings) {
    for (const [nodeId, node] of Object.entries(project.manifest.nodes)) {
        const { nodeType, version } = parseTypeRef(node.type);
        // ── HTTP nodes (no binding required, no library lookup) ──
        if (nodeType === 'core.action.http' || nodeType === 'core.action.http.v2') {
            validateHttpRawInputs(nodeId, node.rawInputs, warnings);
            out.push({ kind: 'http', nodeId, nodeType, rawInputs: node.rawInputs });
            continue;
        }
        // ── Mock nodes ──
        if (nodeType === 'core.logic.mock') {
            // Fixture can live in inputs.fixture, configuration.fixture, rawInputs, or
            // any of several legacy slots. We just preserve whatever the manifest
            // ships and let the dispatcher decide how to render it.
            const fixture = pickMockFixture(node);
            out.push({ kind: 'mock', nodeId, nodeType, fixture });
            continue;
        }
        // ── Script nodes invoked via executeNode ──
        // FIL has two patterns for scripts: __script_<id> sync helpers (inlined
        // into the WASM, never a decision) AND `await executeNode("name", ...)`
        // against a script-typed manifest entry (the host dispatches them so the
        // result is captured in history — required for replay-safe non-determinism
        // like Math.random). We resolve every script node optimistically; if the
        // FIL never references it, no decision ever asks for it.
        if (nodeType === 'core.action.script') {
            const scriptBody = pickScriptBody(node);
            if (scriptBody !== undefined) {
                out.push({ kind: 'script', nodeId, nodeType, scriptBody });
                continue;
            }
            // No body in the manifest — most likely an inlined __script_<id>
            // helper. Fall through to the skip list.
        }
        // ── Agent resource nodes ──
        if (isAgentNodeType(nodeType)) {
            const agentNode = resolveAgentNode(nodeId, nodeType, node, bindingResolver, errors);
            if (agentNode)
                out.push(agentNode);
            continue;
        }
        // ── API Workflow resource nodes ──
        if (isApiWorkflowNodeType(nodeType)) {
            const apiWorkflowNode = resolveApiWorkflowNode(nodeId, nodeType, node, bindingResolver, errors);
            if (apiWorkflowNode)
                out.push(apiWorkflowNode);
            continue;
        }
        // ── RPA Workflow resource nodes ──
        if (isRpaWorkflowNodeType(nodeType)) {
            const rpaWorkflowNode = resolveRpaWorkflowNode(nodeId, nodeType, node, bindingResolver, errors);
            if (rpaWorkflowNode)
                out.push(rpaWorkflowNode);
            continue;
        }
        // ── Inline low-code Agent nodes ──
        if (nodeType === INLINE_AGENT_NODE_TYPE) {
            const inlineAgentNode = resolveInlineAgentNode(nodeId, nodeType, node, errors);
            if (inlineAgentNode)
                out.push(inlineAgentNode);
            continue;
        }
        // ── HITL quickform nodes ──
        if (nodeType === HITL_NODE_TYPE) {
            const hitlNode = resolveHitlNode(nodeId, nodeType, node, errors);
            if (hitlNode)
                out.push(hitlNode);
            continue;
        }
        // ── Summarize pattern nodes ──
        if (nodeType === SUMMARIZE_NODE_TYPE) {
            const summarizeNode = resolveSummarizeNode(nodeId, nodeType, node, errors);
            if (summarizeNode)
                out.push(summarizeNode);
            continue;
        }
        // ── Skip non-dispatchable types (control flow, triggers, end) ──
        // These never produce decisions in the FIL replay protocol; they're either
        // inlined or auto-generated. If FIL emits a decision for one of these,
        // the runner will surface a "no resolved node" error.
        if (nodeType.startsWith('core.trigger.') ||
            nodeType.startsWith('core.control.') ||
            nodeType === 'core.action.script' || // (with no body — inlined __script_<id>)
            nodeType === 'core.logic.decision' ||
            nodeType === 'core.logic.merge' ||
            nodeType === 'core.logic.switch' ||
            nodeType === 'core.logic.loop' ||
            nodeType === 'core.logic.delay' || // becomes executeTimer:, dispatched by kind, not by manifest
            nodeType === 'core.logic.terminate' ||
            nodeType === 'core.subflow') {
            continue;
        }
        // ── Connector nodes ──
        if (!nodeType.startsWith('uipath.connector.')) {
            warnings.push(`node "${nodeId}" (${nodeType}): unrecognized node type, skipping`);
            continue;
        }
        if (!node.binding) {
            errors.push(`node "${nodeId}" (${nodeType}): missing "binding" (bindings.json entry ID for ConnectionId)`);
            continue;
        }
        if (!node.folderBinding) {
            errors.push(`node "${nodeId}" (${nodeType}): missing "folderBinding" (bindings.json entry ID for FolderKey)`);
            continue;
        }
        const connRes = bindingResolver.resolve(node.binding);
        if (connRes.kind === 'missing') {
            const known = bindingResolver.knownIds();
            const suggest = known.length > 0 ? ` Known IDs: ${known.map(k => `"${k}"`).join(', ')}.` : '';
            errors.push(`node "${nodeId}" (${nodeType}): binding "${node.binding}" is not declared in bindings.json.${suggest}`);
            continue;
        }
        if (connRes.kind === 'stub') {
            errors.push(`node "${nodeId}" (${nodeType}): binding "${node.binding}" in bindings.json still has a stub value (${connRes.raw || '<empty>'}). ` +
                `Replace it with a real ConnectionId from \`uip is connections list\`.`);
            continue;
        }
        const folderRes = bindingResolver.resolve(node.folderBinding);
        if (folderRes.kind === 'missing') {
            const known = bindingResolver.knownIds();
            const suggest = known.length > 0 ? ` Known IDs: ${known.map(k => `"${k}"`).join(', ')}.` : '';
            errors.push(`node "${nodeId}" (${nodeType}): folderBinding "${node.folderBinding}" is not declared in bindings.json.${suggest}`);
            continue;
        }
        if (folderRes.kind === 'stub') {
            errors.push(`node "${nodeId}" (${nodeType}): folderBinding "${node.folderBinding}" in bindings.json still has a stub value (${folderRes.raw || '<empty>'}). ` +
                `Replace it with a real FolderKey from \`uip is connections list\`.`);
            continue;
        }
        const lookup = findConnectorDef(libraryDir, nodeType, version);
        if (!lookup) {
            errors.push(`node "${nodeId}" (${nodeType}@${version}): no connector def found under ${libraryDir}`);
            continue;
        }
        const verb = CRUD_VERBS[lookup.def.operation.name];
        if (!verb) {
            errors.push(`node "${nodeId}" (${nodeType}): operation "${lookup.def.operation.name}" is not a CRUD verb ` +
                `(supported: ${Object.keys(CRUD_VERBS).join(', ')}). flow-run doesn't dispatch non-CRUD activities yet.`);
            continue;
        }
        out.push({
            kind: 'connector',
            nodeId,
            nodeType,
            connectorKey: lookup.def.connector.key,
            objectName: lookup.def.operation.objectName,
            operationName: lookup.def.operation.name,
            verb,
            connectionId: connRes.uuid,
            folderKey: folderRes.uuid,
            connectorDefPath: lookup.path,
        });
    }
}
function resolveAgentNode(nodeId, nodeType, node, bindingResolver, errors) {
    const base = resolveProcessResourceNode(nodeId, nodeType, node, bindingResolver, errors, {
        prefix: AGENT_NODE_PREFIX,
        resourceSubType: 'Agent',
        orchestratorType: 'agent',
        serviceType: AGENT_SERVICE_TYPE,
        label: 'Agent',
        articleLabel: 'an Agent',
    });
    return base ? { kind: 'agent', ...base } : null;
}
function resolveApiWorkflowNode(nodeId, nodeType, node, bindingResolver, errors) {
    const base = resolveProcessResourceNode(nodeId, nodeType, node, bindingResolver, errors, {
        prefix: API_WORKFLOW_NODE_PREFIX,
        resourceSubType: 'Api',
        orchestratorType: 'api',
        serviceType: API_WORKFLOW_SERVICE_TYPE,
        label: 'API Workflow',
        articleLabel: 'an API Workflow',
    });
    return base ? { kind: 'api-workflow', ...base } : null;
}
function resolveRpaWorkflowNode(nodeId, nodeType, node, bindingResolver, errors) {
    const base = resolveProcessResourceNode(nodeId, nodeType, node, bindingResolver, errors, {
        prefix: RPA_WORKFLOW_NODE_PREFIX,
        resourceSubType: 'Process',
        orchestratorType: 'process',
        serviceType: RPA_WORKFLOW_SERVICE_TYPE,
        label: 'RPA Workflow',
        articleLabel: 'an RPA Workflow',
    });
    return base ? { kind: 'rpa-workflow', ...base } : null;
}
function resolveProcessResourceNode(nodeId, nodeType, node, bindingResolver, errors, spec) {
    const errorStart = errors.length;
    const resourceKey = node.resource?.resourceKey ?? parseProcessResourceKey(nodeType, spec.prefix);
    if (!node.resource) {
        errors.push(`node "${nodeId}" (${nodeType}): missing "resource" metadata for ${spec.label} node`);
    }
    else {
        if (node.resource.resource !== 'process') {
            errors.push(`node "${nodeId}" (${nodeType}): resource.resource expected "process", got "${node.resource.resource ?? '<missing>'}"`);
        }
        if (node.resource.resourceSubType !== spec.resourceSubType) {
            errors.push(`node "${nodeId}" (${nodeType}): resource.resourceSubType expected "${spec.resourceSubType}", got "${node.resource.resourceSubType ?? '<missing>'}"`);
        }
    }
    const resourceBindings = node.resourceBindings;
    if (!resourceBindings) {
        errors.push(`node "${nodeId}" (${nodeType}): missing "resourceBindings" for ${spec.label} node`);
    }
    const nameBindingId = resourceBindings?.name;
    const folderPathBindingId = resourceBindings?.folderPath;
    if (!nameBindingId) {
        errors.push(`node "${nodeId}" (${nodeType}): missing resourceBindings.name`);
    }
    if (!folderPathBindingId) {
        errors.push(`node "${nodeId}" (${nodeType}): missing resourceBindings.folderPath`);
    }
    const nameBinding = nameBindingId
        ? resolveProcessResourceBinding(nodeId, nodeType, 'name', nameBindingId, resourceKey, bindingResolver, errors, spec)
        : null;
    const folderPathBinding = folderPathBindingId
        ? resolveProcessResourceBinding(nodeId, nodeType, 'folderPath', folderPathBindingId, resourceKey, bindingResolver, errors, spec)
        : null;
    if (errors.length > errorStart ||
        !node.resource ||
        !nameBinding ||
        !folderPathBinding ||
        !nameBindingId ||
        !folderPathBindingId) {
        return null;
    }
    return {
        nodeId,
        nodeType,
        resource: 'process',
        resourceSubType: spec.resourceSubType,
        resourceKey,
        orchestratorType: node.resource.orchestratorType,
        serviceType: node.resource.serviceType ?? spec.serviceType,
        section: node.resource.section,
        nameBinding: nameBindingId,
        folderPathBinding: folderPathBindingId,
        name: nameBinding.value,
        folderPath: folderPathBinding.value,
        fixture: pickMockFixture(node),
    };
}
function resolveInlineAgentNode(nodeId, nodeType, node, errors) {
    const errorStart = errors.length;
    const inputs = inlineAgentInputs(node);
    const source = requireInlineString(nodeId, nodeType, inputs, 'source', errors);
    if (source && isPlaceholderValue(source)) {
        errors.push(`node "${nodeId}" (${nodeType}): rawInputs.source still has a placeholder value (${source}). ` +
            `Use the inline Agent sidecar project id.`);
    }
    const systemPrompt = requireInlineString(nodeId, nodeType, inputs, 'systemPrompt', errors);
    const userPrompt = requireInlineString(nodeId, nodeType, inputs, 'userPrompt', errors);
    const model = requireInlineString(nodeId, nodeType, inputs, 'model', errors);
    const agentInputVariables = requireInlineArray(nodeId, nodeType, inputs, 'agentInputVariables', errors);
    const agentOutputVariables = requireInlineArray(nodeId, nodeType, inputs, 'agentOutputVariables', errors);
    if (agentOutputVariables && agentOutputVariables.length === 0) {
        errors.push(`node "${nodeId}" (${nodeType}): rawInputs.agentOutputVariables must contain at least one output variable`);
    }
    if (errors.length > errorStart ||
        !source ||
        !systemPrompt ||
        !userPrompt ||
        !model ||
        !agentInputVariables ||
        !agentOutputVariables) {
        return null;
    }
    return {
        kind: 'inline-agent',
        nodeId,
        nodeType,
        source,
        serviceType: INLINE_AGENT_SERVICE_TYPE,
        systemPrompt,
        userPrompt,
        model,
        agentInputVariables,
        agentOutputVariables,
        fixture: pickMockFixture(node),
    };
}
function inlineAgentInputs(node) {
    return node.rawInputs ?? node.inputs ?? {};
}
function resolveHitlNode(nodeId, nodeType, node, errors) {
    const inputs = node.rawInputs ?? node.inputs ?? {};
    if (inputs.type !== 'quick') {
        errors.push(`node "${nodeId}" (${nodeType}): only HITL quickform inputs.type="quick" is supported in Flow v2 dry-run`);
        return null;
    }
    const schema = inputs.schema;
    if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
        errors.push(`node "${nodeId}" (${nodeType}): missing rawInputs.schema for HITL quickform`);
        return null;
    }
    return {
        kind: 'hitl',
        nodeId,
        nodeType,
        taskType: 'quick',
        schema: schema,
        recipient: inputs.recipient,
        priority: typeof inputs.priority === 'string' ? inputs.priority : undefined,
        fixture: pickMockFixture(node),
    };
}
function resolveSummarizeNode(nodeId, nodeType, node, errors) {
    const errorStart = errors.length;
    const inputs = node.rawInputs ?? node.inputs ?? {};
    const attachment = requireInlineString(nodeId, nodeType, inputs, 'attachment', errors);
    const prompt = requireInlineString(nodeId, nodeType, inputs, 'prompt', errors);
    const rawReturnCitations = inputs.returnCitations;
    let returnCitations = false;
    if (rawReturnCitations !== undefined) {
        if (typeof rawReturnCitations !== 'boolean') {
            errors.push(`node "${nodeId}" (${nodeType}): rawInputs.returnCitations must be a boolean when provided`);
        }
        else {
            returnCitations = rawReturnCitations;
        }
    }
    if (errors.length > errorStart || !attachment || !prompt)
        return null;
    return {
        kind: 'summarize',
        nodeId,
        nodeType,
        attachment,
        prompt,
        returnCitations,
        fixture: pickMockFixture(node),
    };
}
function requireInlineString(nodeId, nodeType, inputs, field, errors) {
    const value = inputs[field];
    if (typeof value !== 'string' || value.trim() === '') {
        errors.push(`node "${nodeId}" (${nodeType}): missing rawInputs.${field}`);
        return null;
    }
    return value;
}
function requireInlineArray(nodeId, nodeType, inputs, field, errors) {
    const value = inputs[field];
    if (!Array.isArray(value)) {
        errors.push(`node "${nodeId}" (${nodeType}): missing rawInputs.${field}`);
        return null;
    }
    return value;
}
function resolveProcessResourceBinding(nodeId, nodeType, propertyAttribute, bindingId, resourceKey, bindingResolver, errors, spec) {
    const entry = bindingResolver.get(bindingId);
    if (!entry) {
        const known = bindingResolver.knownIds();
        const suggest = known.length > 0 ? ` Known IDs: ${known.map(k => `"${k}"`).join(', ')}.` : '';
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" is not declared in bindings.json.${suggest}`);
        return null;
    }
    if (entry.resource !== 'process') {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" expected resource "process", got "${entry.resource ?? '<missing>'}"`);
    }
    if (entry.resourceSubType !== spec.resourceSubType) {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" expected resourceSubType "${spec.resourceSubType}", got "${entry.resourceSubType ?? '<missing>'}"`);
    }
    if (entry.propertyAttribute !== propertyAttribute) {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" expected propertyAttribute "${propertyAttribute}", got "${entry.propertyAttribute ?? '<missing>'}"`);
    }
    if (resourceKey && typeof entry.resourceKey === 'string' && entry.resourceKey && entry.resourceKey !== resourceKey) {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" resourceKey "${entry.resourceKey}" does not match ${spec.label} resourceKey "${resourceKey}"`);
    }
    const value = bindingDefault(entry);
    if (isPlaceholderValue(value)) {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" in bindings.json still has a placeholder value (${value}). ` +
            `Replace it with the ${spec.label} ${propertyAttribute}.`);
    }
    if (propertyAttribute === 'name' && value.trim() === '') {
        errors.push(`node "${nodeId}" (${nodeType}): resourceBinding "${bindingId}" must provide ${spec.articleLabel} name in default`);
    }
    return { entry, value };
}
function bindingDefault(entry) {
    if (typeof entry.default === 'string')
        return entry.default;
    if (typeof entry.resourceKey === 'string')
        return entry.resourceKey;
    return '';
}
function isPlaceholderValue(value) {
    const trimmed = value.trim();
    return trimmed.startsWith('<') && trimmed.endsWith('>');
}
/**
 * Pick a mock fixture from a manifest node — first available slot wins.
 *
 * Preferred: the top-level `fixture` field (set when the FIL declares
 * `action <name>: mock { fixture: { … } };`).
 *
 * Fallbacks accept the legacy v1 shapes — `rawInputs`/`inputs`/`configuration`
 * with a `fixture` / `output` / `value` sub-key — so existing v1 .flow files
 * round-tripped through v1-to-v2 still resolve correctly.
 */
function pickMockFixture(node) {
    if (node.fixture !== undefined)
        return node.fixture;
    const sources = [node.configuration, node.inputs, node.rawInputs];
    for (const src of sources) {
        if (!src)
            continue;
        if ('fixture' in src)
            return src.fixture;
        if ('output' in src)
            return src.output;
        if ('value' in src)
            return src.value;
    }
    return undefined;
}
/** Pick a script body from a manifest node — typically lives at rawInputs.script. */
function pickScriptBody(node) {
    const sources = [node.rawInputs, node.inputs];
    for (const src of sources) {
        if (src && typeof src.script === 'string')
            return src.script;
    }
    return undefined;
}
/**
 * Pre-flight validation for an HTTP node's manifest `rawInputs`. We can't
 * fully validate the *merged* shape at pre-flight (FIL's executeNode inputs
 * aren't known until dispatch), but we can flag obvious manifest-side issues:
 * type mismatches on known fields, and the case where rawInputs alone has
 * no `url` (the FIL will be required to provide one). The dispatcher does
 * the strict required-field check on the merged result.
 */
function validateHttpRawInputs(nodeId, rawInputs, warnings) {
    if (!rawInputs)
        return;
    const typed = [
        ['url', 'string'], ['method', 'string'], ['contentType', 'string'],
        ['timeout', 'string'], ['retryCount', 'number'],
    ];
    for (const [field, expected] of typed) {
        if (field in rawInputs && rawInputs[field] !== undefined && rawInputs[field] !== null) {
            const actual = typeof rawInputs[field];
            if (actual !== expected) {
                warnings.push(`node "${nodeId}" (HTTP): manifest rawInputs.${field} is ${actual}, expected ${expected}`);
            }
        }
    }
    for (const field of ['headers', 'queryParams']) {
        const v = rawInputs[field];
        if (v !== undefined && v !== null && (typeof v !== 'object' || Array.isArray(v))) {
            warnings.push(`node "${nodeId}" (HTTP): manifest rawInputs.${field} should be an object, got ${typeof v}`);
        }
    }
}
function parseTypeRef(typeRef) {
    const at = typeRef.lastIndexOf('@');
    if (at === -1)
        return { nodeType: typeRef, version: '1.0.0' };
    return { nodeType: typeRef.slice(0, at), version: typeRef.slice(at + 1) };
}
function isAgentNodeType(nodeType) {
    return nodeType.startsWith(AGENT_NODE_PREFIX);
}
function isApiWorkflowNodeType(nodeType) {
    return nodeType.startsWith(API_WORKFLOW_NODE_PREFIX);
}
function isRpaWorkflowNodeType(nodeType) {
    return nodeType.startsWith(RPA_WORKFLOW_NODE_PREFIX);
}
function parseProcessResourceKey(nodeType, prefix) {
    if (!nodeType.startsWith(prefix))
        return undefined;
    return nodeType.slice(prefix.length) || undefined;
}
function isStubUuid(uuid) {
    // Real UUIDs from uip are random. Two stub forms we explicitly recognize:
    //   1. Legacy zero-prefixed: `00000000-0000-0000-0000-XXXXXXXXXXXX`
    //   2. Bracketed placeholder: `<slack-connection-id>` etc. — used in the
    //      authoring template so agents see what to substitute rather than
    //      looking-real-but-zero UUIDs.
    const v = uuid.trim().toLowerCase();
    if (v.startsWith(ZERO_UUID_PREFIX))
        return true;
    if (v.startsWith('<') && v.endsWith('>'))
        return true;
    return false;
}
// ─── Connector library lookup ────────────────────────────────────────────────
function findConnectorDef(libraryDir, nodeType, version) {
    const m = nodeType.match(/^uipath\.connector\.([^.]+)\.(.+)$/);
    if (!m)
        return null;
    const [, connectorKey, opSlug] = m;
    const filename = `${opSlug}@${version}.json`;
    const fullPath = path.join(libraryDir, connectorKey, filename);
    if (!fs.existsSync(fullPath))
        return null;
    try {
        const def = JSON.parse(fs.readFileSync(fullPath, 'utf8'));
        return { def, path: fullPath };
    }
    catch {
        return null;
    }
}
// ─── Connection verification ─────────────────────────────────────────────────
function verifyConnections(nodes, connections, errors, warnings) {
    const byId = new Map();
    for (const c of connections)
        byId.set(c.Id.toLowerCase(), c);
    const nodesByConn = new Map();
    for (const n of nodes) {
        const key = n.connectionId.toLowerCase();
        if (!nodesByConn.has(key))
            nodesByConn.set(key, []);
        nodesByConn.get(key).push(n);
    }
    for (const [connId, nodesForConn] of nodesByConn) {
        const conn = byId.get(connId);
        const exemplar = nodesForConn[0];
        const nodeNames = nodesForConn.map(n => `"${n.nodeId}"`).join(', ');
        if (!conn) {
            const candidates = connections.filter(c => c.ConnectorKey === exemplar.connectorKey);
            const hint = candidates.length > 0
                ? `\n    candidates for ${exemplar.connectorKey}:\n` +
                    candidates.map(c => `      - ${c.Id}  ${c.Name} (folder: ${c.FolderKey})`).join('\n')
                : `\n    (you have no connections for ${exemplar.connectorKey} — create one with: uip is connections create)`;
            errors.push(`connection ${connId} (used by ${nodeNames}) not found in your tenant.${hint}`);
            continue;
        }
        if (conn.State !== 'Enabled') {
            warnings.push(`connection ${connId} (${conn.Name}) state is "${conn.State}", not Enabled`);
        }
        if (conn.ConnectorKey !== exemplar.connectorKey) {
            errors.push(`connection ${connId} is for connector "${conn.ConnectorKey}" but ${nodeNames} expect "${exemplar.connectorKey}"`);
            continue;
        }
        for (const n of nodesForConn) {
            if (n.folderKey && n.folderKey.toLowerCase() !== conn.FolderKey.toLowerCase()) {
                warnings.push(`node "${n.nodeId}": folderBinding ${n.folderKey} doesn't match connection ${connId}'s folder ${conn.FolderKey}`);
            }
        }
    }
}
//# sourceMappingURL=loader.js.map