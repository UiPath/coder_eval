"use strict";
/**
 * Build the in-memory Flow v2 manifest from collected FlowOverrides
 * + the canonical connector library.
 *
 * The manifest is now an intermediate: it carries the varying parts of each
 * node (per-instance config, bindings, ui) while delegating the static parts
 * (BPMN model, output schema, icons, descriptions) to the library. The
 * converter then bakes this data into FIL action/trigger declarations.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.buildManifest = buildManifest;
const configuration_1 = require("./configuration");
function buildManifest(overrides, library, opts = {}) {
    const manifest = {
        schemaVersion: '1',
        flowId: overrides.flowId,
        flowName: overrides.flowName,
        flowVersion: overrides.flowVersion,
        nodes: {},
    };
    if (overrides.solutionId)
        manifest.solutionId = overrides.solutionId;
    if (overrides.projectId)
        manifest.projectId = overrides.projectId;
    if (overrides.metadata?.description)
        manifest.description = overrides.metadata.description;
    if (overrides.variableOverrides)
        manifest.variables = overrides.variableOverrides;
    const referenced = new Set();
    const unresolved = [];
    // Build a UUID → binding-id index from the v1 bindings[] so compressNode
    // can replace each connectionId/connectionFolderKey UUID with its symbolic
    // binding ID. Index both `resourceKey` and `default` — the values often
    // match but can diverge, and either could be the one quoted by a node's
    // inputs.detail. v2→v1 prefers `default` when emitting (see manifest.ts in
    // v2-to-v1).
    const uuidToBindingId = new Map();
    const resourceBindingIdByKeyAndAttribute = new Map();
    for (const raw of overrides.bindings ?? []) {
        const b = raw;
        if (typeof b.id !== 'string')
            continue;
        if (typeof b.default === 'string')
            uuidToBindingId.set(b.default.toLowerCase(), b.id);
        if (typeof b.resourceKey === 'string')
            uuidToBindingId.set(b.resourceKey.toLowerCase(), b.id);
        if (typeof b.resourceKey === 'string' && typeof b.propertyAttribute === 'string') {
            const resource = typeof b.resource === 'string' ? b.resource : '';
            resourceBindingIdByKeyAndAttribute.set(resourceBindingKey(resource, b.resourceKey, b.propertyAttribute), b.id);
        }
    }
    for (const [nodeId, override] of Object.entries(overrides.nodeOverrides)) {
        const node = compressNode(nodeId, override, library, referenced, unresolved, uuidToBindingId, resourceBindingIdByKeyAndAttribute, opts);
        if (node)
            manifest.nodes[nodeId] = node;
    }
    return { manifest, referencedEntries: referenced, unresolvedTypes: unresolved };
}
/** Compress one v1 node override into a v2 manifest entry. */
function compressNode(nodeId, override, library, referenced, unresolved, uuidToBindingId, resourceBindingIdByKeyAndAttribute, opts) {
    const type = override.type;
    if (!type)
        return undefined; // shouldn't happen — flow2fil always sets it
    const version = override.typeVersion ?? '1.0.0';
    const typeRef = `${type}@${version}`;
    const node = { type: typeRef };
    // UI: keep position only (size is usually default; drop unless overridden)
    if (override.ui?.position) {
        node.ui = { position: override.ui.position };
        if (override.ui.size && (override.ui.size.width !== 96 || override.ui.size.height !== 96)) {
            node.ui.size = override.ui.size;
        }
    }
    // Outputs: forward — these carry the var bindings the FIL/runtime needs.
    if (override.outputs && Object.keys(override.outputs).length > 0) {
        node.outputs = override.outputs;
    }
    // For integration nodes, look up the library entry and strip what matches.
    const isIntegration = type.startsWith('uipath.connector.');
    if (isIntegration) {
        const lib = library.lookup(type, version);
        if (!lib) {
            unresolved.push(typeRef);
            // Fall through to "raw" handling so we don't lose data.
        }
        else {
            referenced.add(typeRef);
            compressIntegrationNode(node, override, lib, typeRef, uuidToBindingId, opts);
            return node;
        }
    }
    if (isProcessResourceNodeType(type)) {
        compressProcessResourceNode(node, override, type, resourceBindingIdByKeyAndAttribute);
    }
    if (isQueueNodeType(type)) {
        compressQueueNode(node, override, type, resourceBindingIdByKeyAndAttribute);
    }
    // Non-integration nodes (control flow, custom actions, agents, etc.) have
    // no canonical library entry. Preserve their raw inputs blob verbatim and
    // strip only the obvious display defaults.
    if (override.display?.label)
        node.label = override.display.label;
    if (override.inputs)
        node.rawInputs = override.inputs;
    if ((type === 'core.logic.mock' || type === 'uipath.human-in-the-loop') && node.rawInputs) {
        promoteDryRunFixture(node);
    }
    return node;
}
function promoteDryRunFixture(node) {
    const r = node.rawInputs;
    if (!r)
        return;
    let extracted;
    if ('fixture' in r) {
        extracted = r.fixture;
        delete r.fixture;
    }
    else if ('output' in r) {
        extracted = r.output;
        delete r.output;
    }
    else if ('value' in r) {
        extracted = r.value;
        delete r.value;
    }
    if (extracted !== undefined) {
        node.fixture = extracted;
        if (Object.keys(r).length === 0)
            delete node.rawInputs;
    }
}
function compressProcessResourceNode(node, override, nodeType, resourceBindingIdByKeyAndAttribute) {
    const spec = processResourceSpec(nodeType);
    if (!spec)
        return;
    const model = override.model;
    const bindingModel = model?.bindings;
    const resourceKey = typeof bindingModel?.resourceKey === 'string'
        ? bindingModel.resourceKey
        : parseProcessResourceKey(nodeType);
    const resourceSubType = typeof bindingModel?.resourceSubType === 'string'
        ? bindingModel.resourceSubType
        : spec.resourceSubType;
    const orchestratorType = typeof bindingModel?.orchestratorType === 'string'
        ? bindingModel.orchestratorType
        : spec.orchestratorType;
    node.resource = {
        resource: typeof bindingModel?.resource === 'string' ? bindingModel.resource : 'process',
        resourceSubType,
        resourceKey,
        serviceType: typeof model?.serviceType === 'string' ? model.serviceType : spec.serviceType,
        ...(orchestratorType ? { orchestratorType } : {}),
    };
    if (typeof model?.section === 'string') {
        node.resource.section = model.section;
    }
    const resourceBindings = {};
    const nameBinding = findResourceBindingId('process', resourceKey, 'name', model, resourceBindingIdByKeyAndAttribute);
    const folderBinding = findResourceBindingId('process', resourceKey, 'folderPath', model, resourceBindingIdByKeyAndAttribute);
    if (nameBinding)
        resourceBindings.name = nameBinding;
    if (folderBinding)
        resourceBindings.folderPath = folderBinding;
    if (Object.keys(resourceBindings).length > 0) {
        node.resourceBindings = resourceBindings;
    }
}
const QUEUE_NODE_SPECS = [
    {
        nodeType: 'core.action.queue.create',
        serviceType: 'Orchestrator.CreateQueueItem',
    },
    {
        nodeType: 'core.action.queue.create-and-wait',
        serviceType: 'Orchestrator.CreateAndWaitForQueueItem',
    },
];
function queueNodeSpec(nodeType) {
    return QUEUE_NODE_SPECS.find((spec) => spec.nodeType === nodeType);
}
function isQueueNodeType(nodeType) {
    return queueNodeSpec(nodeType) !== undefined;
}
function compressQueueNode(node, override, nodeType, resourceBindingIdByKeyAndAttribute) {
    const spec = queueNodeSpec(nodeType);
    if (!spec)
        return;
    const model = override.model;
    const rawInputs = override.inputs;
    const queueInput = rawInputs?.queue;
    const queueRecord = queueInput && typeof queueInput === 'object' && !Array.isArray(queueInput)
        ? queueInput
        : undefined;
    const bindingModel = model?.bindings;
    const resourceKey = typeof bindingModel?.resourceKey === 'string' && bindingModel.resourceKey !== ''
        ? bindingModel.resourceKey
        : typeof queueRecord?.key === 'string'
            ? queueRecord.key
            : undefined;
    node.resource = {
        resource: typeof bindingModel?.resource === 'string' ? bindingModel.resource : 'queue',
        ...(resourceKey ? { resourceKey } : {}),
        serviceType: typeof model?.serviceType === 'string' ? model.serviceType : spec.serviceType,
    };
    const resourceBindings = {};
    const nameBinding = findResourceBindingId('queue', resourceKey, 'name', model, resourceBindingIdByKeyAndAttribute);
    const folderBinding = findResourceBindingId('queue', resourceKey, 'folderPath', model, resourceBindingIdByKeyAndAttribute);
    if (nameBinding)
        resourceBindings.name = nameBinding;
    if (folderBinding)
        resourceBindings.folderPath = folderBinding;
    if (Object.keys(resourceBindings).length > 0) {
        node.resourceBindings = resourceBindings;
    }
}
const PROCESS_RESOURCE_SPECS = [
    {
        prefix: 'uipath.core.agent.',
        resourceSubType: 'Agent',
        orchestratorType: 'agent',
        serviceType: 'Orchestrator.StartAgentJob',
    },
    {
        prefix: 'uipath.core.api-workflow.',
        resourceSubType: 'Api',
        orchestratorType: 'api',
        serviceType: 'Orchestrator.ExecuteApiWorkflowAsync',
    },
    {
        prefix: 'uipath.core.rpa-workflow.',
        resourceSubType: 'Process',
        orchestratorType: 'process',
        serviceType: 'Orchestrator.StartJob',
    },
    {
        prefix: 'uipath.core.agentic-process.',
        resourceSubType: 'ProcessOrchestration',
        serviceType: 'Orchestrator.StartAgenticProcessAsync',
    },
];
function processResourceSpec(nodeType) {
    return PROCESS_RESOURCE_SPECS.find((spec) => nodeType.startsWith(spec.prefix));
}
function isProcessResourceNodeType(nodeType) {
    return processResourceSpec(nodeType) !== undefined;
}
function parseProcessResourceKey(nodeType) {
    const spec = processResourceSpec(nodeType);
    return spec ? nodeType.slice(spec.prefix.length) : undefined;
}
function findResourceBindingId(resource, resourceKey, propertyAttribute, model, resourceBindingIdByKeyAndAttribute) {
    if (resourceKey) {
        const fromBinding = resourceBindingIdByKeyAndAttribute.get(resourceBindingKey(resource, resourceKey, propertyAttribute));
        if (fromBinding)
            return fromBinding;
    }
    const fromContext = model?.context
        ?.find((entry) => entry.name === propertyAttribute && typeof entry.value === 'string')
        ?.value;
    const match = fromContext?.match(/^=bindings\.(.+)$/);
    return match?.[1];
}
function resourceBindingKey(resource, resourceKey, propertyAttribute) {
    return `${resource.toLowerCase()}\0${resourceKey}\0${propertyAttribute.toLowerCase()}`;
}
/**
 * Compress a v1 integration node by stripping fields that are deterministic
 * given the canonical library entry. What remains is the per-instance config.
 */
function compressIntegrationNode(node, override, lib, typeRef, uuidToBindingId, opts) {
    // Display label: keep only if it differs from the library default.
    if (override.display?.label && override.display.label !== lib.display.label) {
        node.label = override.display.label;
    }
    const detail = override.inputs?.detail ?? {};
    // Bindings — replace UUIDs with the v1 bindings[].id symbol so the manifest
    // references the binding by name and bindings.json stays the single source
    // of truth for the real UUIDs.
    if (typeof detail.connectionId === 'string') {
        node.binding = uuidToBindingId.get(detail.connectionId.toLowerCase()) ?? detail.connectionId;
    }
    if (typeof detail.connectionFolderKey === 'string') {
        node.folderBinding = uuidToBindingId.get(detail.connectionFolderKey.toLowerCase()) ?? detail.connectionFolderKey;
    }
    // First pass: top-level `inputs.detail` fields. The big `configuration`
    // string is handled separately via distillConfiguration.
    const residualInputs = {};
    let configurationString;
    for (const [k, v] of Object.entries(detail)) {
        if (LIBRARY_DETERMINED_FIELDS.has(k))
            continue;
        if (k === 'connector' && v === lib.connector.key)
            continue;
        if (k === 'method' && v === lib.operation.httpMethod)
            continue;
        if (k === 'endpoint') {
            const expected = lib.operation.pathTemplate ?? lib.operation.path;
            if (expected && v === expected)
                continue;
        }
        if (k === 'connectionId' || k === 'connectionResourceId' || k === 'connectionFolderKey') {
            continue; // already in node.binding / node.folderBinding
        }
        if (k === 'configuration' && typeof v === 'string') {
            configurationString = v;
            continue;
        }
        residualInputs[k] = v;
    }
    if (Object.keys(residualInputs).length > 0) {
        node.inputs = residualInputs;
    }
    // Second pass: distill the `configuration` blob.
    if (configurationString !== undefined) {
        const parsed = parseConfigurationString(configurationString);
        if (parsed) {
            const result = (0, configuration_1.distillConfiguration)(parsed, {
                cache: opts.fieldsCache,
                source: opts.source,
                typeRef,
                library: lib,
            });
            if (Object.keys(result.kept).length > 0) {
                node.configuration = result.kept;
            }
            if (Object.keys(result.extras).length > 0) {
                node.configurationExtras = result.extras;
            }
        }
        else {
            // Couldn't parse — preserve verbatim so we don't lose data.
            node.configuration = { _verbatim: configurationString };
        }
    }
}
/**
 * Parses the v1 `inputs.detail.configuration` field. The expression form is
 * `=jsonString:{...}` (and rarely a bare JSON string). Returns null if neither
 * shape matches; the caller falls back to verbatim preservation.
 */
function parseConfigurationString(raw) {
    let body = raw;
    const prefix = '=jsonString:';
    if (body.startsWith(prefix))
        body = body.slice(prefix.length);
    try {
        const parsed = JSON.parse(body);
        return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
            ? parsed
            : null;
    }
    catch {
        return null;
    }
}
/**
 * Fields that always match the library entry and can be dropped from a v1
 * integration node's `inputs.detail`. Anything not in this set is kept (we
 * err on the side of not losing data on the first pass).
 *
 * `inputMetadata`, `telemetryData`, and `errorState` are editor-only state
 * that doesn't affect runtime behavior; they're safe to drop.
 *
 * `uiPathActivityTypeId` is a stable per-(connector, action) identifier —
 * always the same for a given nodeType. The canonical library doesn't yet
 * carry it, but it's deterministic so dropping is fine for v1→v2; on v2→v1
 * we'll need to either look it up or extend the library.
 */
const LIBRARY_DETERMINED_FIELDS = new Set([
    'inputMetadata',
    'telemetryData',
    'errorState',
    'uiPathActivityTypeId',
    // 'configuration' is NOT in this list — it carries per-instance values
    // mixed with library data. Compression is a future pass.
]);
//# sourceMappingURL=manifest.js.map