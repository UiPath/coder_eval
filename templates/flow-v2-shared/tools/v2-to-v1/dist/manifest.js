"use strict";
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
Object.defineProperty(exports, "__esModule", { value: true });
exports.expandManifest = expandManifest;
const configuration_1 = require("./configuration");
function buildBindingLookup(bindings) {
    const byId = new Map();
    const entriesById = new Map();
    for (const b of bindings?.bindings ?? []) {
        // v1's connectionId/connectionFolderKey fields are populated from
        // `default` (the design-time bound value), not `resourceKey` (the
        // resource lookup id). They're often the same UUID, but when they
        // diverge the v1 round-trip requires `default`.
        const raw = (typeof b.default === 'string' && b.default)
            || (typeof b.resourceKey === 'string' && b.resourceKey)
            || '';
        if (b.id && raw)
            byId.set(b.id, raw);
        if (b.id)
            entriesById.set(b.id, b);
    }
    return {
        resolve: (id) => byId.get(id),
        get: (id) => entriesById.get(id),
    };
}
function expandManifest(manifest, opts) {
    const overrides = {};
    const typeRefs = new Set();
    const missing = [];
    const bindingLookup = buildBindingLookup(opts.bindings);
    for (const [nodeId, node] of Object.entries(manifest.nodes)) {
        const { typeRef, override } = expandNode(nodeId, node, opts, bindingLookup, missing);
        overrides[nodeId] = override;
        typeRefs.add(typeRef);
    }
    return { overrides, typeRefs, missingLibraryEntries: [...new Set(missing)] };
}
function expandNode(nodeId, node, opts, bindingLookup, missing) {
    const typeRef = node.type;
    const [type, version = '1.0.0'] = typeRef.split('@');
    const override = { type, typeVersion: version };
    if (node.ui)
        override.ui = node.ui;
    if (node.outputs) {
        // The v2 manifest types `outputs` slightly looser than flow2fil's
        // NodeOverride; cast through unknown to avoid widening errors at the
        // boundary.
        override.outputs = node.outputs;
    }
    const isIntegration = type.startsWith('uipath.connector.');
    if (!isIntegration) {
        if (isProcessResourceNodeType(type) && node.resource) {
            override.model = buildProcessResourceModel(type, node, bindingLookup);
        }
        if (isQueueNodeType(type)) {
            override.model = buildQueueModel(type, node, bindingLookup);
        }
        // Non-integration: rawInputs is the v1 inputs blob, captured verbatim.
        if (node.rawInputs) {
            override.inputs = isQueueNodeType(type)
                ? normalizeQueueInputs(node.rawInputs)
                : node.rawInputs;
        }
        // `core.logic.mock` nodes carry their fixture under the top-level
        // `fixture` field in v2. v1's shape buries it inside the node's
        // `inputs` (as `inputs.fixture`); restore that shape on the way out.
        if (type === 'core.logic.mock' && node.fixture !== undefined) {
            const merged = override.inputs ?? {};
            merged.fixture = node.fixture;
            override.inputs = merged;
        }
        if (node.label) {
            override.display = { label: node.label };
        }
        return { typeRef, override };
    }
    // Integration node — needs the canonical library entry to rehydrate.
    const lib = opts.library.lookup(type, version);
    if (!lib) {
        missing.push(typeRef);
        // Best-effort: if the manifest happens to have rawInputs (it shouldn't
        // for integrations, but defensive), use it; otherwise produce an
        // intentionally-incomplete inputs object that will fail flow validate
        // and surface the missing-library issue.
        if (node.rawInputs)
            override.inputs = node.rawInputs;
        return { typeRef, override };
    }
    // Display: library default + any per-node label override.
    // NodeDisplay omits `description` in the flow2fil types — the field does
    // appear in real .flow files, so we tunnel it via `as unknown as`.
    const display = {
        label: node.label || lib.display.label,
        description: lib.display.description,
        icon: lib.display.icon,
        iconBackground: lib.display.iconBackground,
        iconBackgroundDark: lib.display.iconBackgroundDark,
    };
    override.display = display;
    // Rebuild the giant inputs.detail block.
    const detail = {
        connector: lib.connector.key,
        method: lib.operation.httpMethod,
        endpoint: lib.operation.pathTemplate || lib.operation.path || '',
        inputMetadata: {},
        errorState: { hasError: false },
        telemetryData: {
            connectorKey: lib.connector.key,
            connectorName: lib.connector.name || '',
        },
    };
    // Bindings — the manifest holds symbolic IDs (e.g. "bOutlook"). v1 wants
    // the real UUIDs in three slots: connectionId, connectionResourceId,
    // connectionFolderKey. Resolve through bindings.json.
    if (node.binding) {
        const uuid = bindingLookup.resolve(node.binding);
        if (uuid) {
            detail.connectionId = uuid;
            detail.connectionResourceId = uuid;
        }
        else {
            // No matching bindings.json entry — preserve the symbolic ID so v1
            // validate surfaces a recognizable error rather than silently dropping.
            detail.connectionId = node.binding;
            detail.connectionResourceId = node.binding;
        }
    }
    if (node.folderBinding) {
        const uuid = bindingLookup.resolve(node.folderBinding);
        detail.connectionFolderKey = uuid ?? node.folderBinding;
    }
    // Residual per-instance inputs (queryParameters, multipartParameters,
    // pathParameters, etc.) — the v2 distillation kept these in node.inputs.
    if (node.inputs) {
        Object.assign(detail, node.inputs);
    }
    // Rehydrate the configuration JSON-string.
    const rehydrated = (0, configuration_1.rehydrateConfiguration)({
        library: lib,
        typeRef,
        fieldsCache: opts.fieldsCache,
        configuration: node.configuration,
        extras: node.configurationExtras,
    });
    detail.configuration = rehydrated.configString;
    override.inputs = { detail };
    return { typeRef, override };
}
const PROCESS_RESOURCE_SPECS = [
    {
        prefix: 'uipath.core.agent.',
        resourceSubType: 'Agent',
        orchestratorType: 'agent',
        serviceType: 'Orchestrator.StartAgentJob',
        label: 'Agent',
    },
    {
        prefix: 'uipath.core.api-workflow.',
        resourceSubType: 'Api',
        orchestratorType: 'api',
        serviceType: 'Orchestrator.ExecuteApiWorkflowAsync',
        label: 'API Workflow',
    },
    {
        prefix: 'uipath.core.rpa-workflow.',
        resourceSubType: 'Process',
        orchestratorType: 'process',
        serviceType: 'Orchestrator.StartJob',
        label: 'RPA Workflow',
    },
    {
        prefix: 'uipath.core.agentic-process.',
        resourceSubType: 'ProcessOrchestration',
        serviceType: 'Orchestrator.StartAgenticProcessAsync',
        label: 'Agentic Process',
        modelType: 'bpmn:CallActivity',
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
function buildProcessResourceModel(nodeType, node, bindingLookup) {
    const spec = processResourceSpec(nodeType);
    const resource = node.resource ?? { resource: 'process', resourceSubType: spec?.resourceSubType };
    const resourceBindings = node.resourceBindings ?? {};
    const resourceKey = typeof resource.resourceKey === 'string'
        ? resource.resourceKey
        : parseProcessResourceKey(nodeType);
    const nameBinding = resourceBindings.name ? bindingLookup.get(resourceBindings.name) : undefined;
    const folderBinding = resourceBindings.folderPath ? bindingLookup.get(resourceBindings.folderPath) : undefined;
    const nameDefault = bindingDefault(nameBinding);
    const folderPathDefault = bindingDefault(folderBinding);
    const label = node.label ?? nameDefault ?? spec?.label ?? 'Resource';
    const orchestratorType = resource.orchestratorType ?? spec?.orchestratorType;
    return {
        type: spec?.modelType ?? 'bpmn:ServiceTask',
        serviceType: resource.serviceType ?? spec?.serviceType,
        version: 'v2',
        ...(resource.section ? { section: resource.section } : {}),
        debug: { runtime: 'bpmnEngine' },
        bindings: {
            resource: resource.resource ?? 'process',
            resourceSubType: resource.resourceSubType ?? spec?.resourceSubType,
            ...(resourceKey ? { resourceKey } : {}),
            ...(orchestratorType ? { orchestratorType } : {}),
            values: {
                name: nameDefault ?? '',
                folderPath: folderPathDefault ?? '',
            },
        },
        context: [
            {
                name: 'name',
                type: 'string',
                value: resourceBindings.name ? `=bindings.${resourceBindings.name}` : '<bindings.name>',
                ...(nameDefault !== undefined ? { default: nameDefault } : {}),
            },
            {
                name: 'folderPath',
                type: 'string',
                value: resourceBindings.folderPath ? `=bindings.${resourceBindings.folderPath}` : '<bindings.folderPath>',
                ...(folderPathDefault !== undefined ? { default: folderPathDefault } : {}),
            },
            {
                name: '_label',
                type: 'string',
                value: label,
            },
        ],
    };
}
function bindingDefault(binding) {
    if (!binding)
        return undefined;
    if (typeof binding.default === 'string')
        return binding.default;
    if (typeof binding.resourceKey === 'string')
        return binding.resourceKey;
    return undefined;
}
const QUEUE_NODE_SPECS = [
    {
        nodeType: 'core.action.queue.create',
        modelType: 'bpmn:SendTask',
        serviceType: 'Orchestrator.CreateQueueItem',
        label: 'Create queue item',
    },
    {
        nodeType: 'core.action.queue.create-and-wait',
        modelType: 'bpmn:ServiceTask',
        serviceType: 'Orchestrator.CreateAndWaitForQueueItem',
        label: 'Create and wait for queue item',
    },
];
function queueNodeSpec(nodeType) {
    return QUEUE_NODE_SPECS.find((spec) => spec.nodeType === nodeType);
}
function isQueueNodeType(nodeType) {
    return queueNodeSpec(nodeType) !== undefined;
}
function buildQueueModel(nodeType, node, bindingLookup) {
    const spec = queueNodeSpec(nodeType);
    const resource = node.resource ?? { resource: 'queue' };
    const resourceBindings = node.resourceBindings ?? {};
    const rawInputs = node.rawInputs ?? {};
    const queueInput = rawInputs.queue && typeof rawInputs.queue === 'object' && !Array.isArray(rawInputs.queue)
        ? rawInputs.queue
        : undefined;
    const resourceKey = typeof resource.resourceKey === 'string'
        ? resource.resourceKey
        : typeof queueInput?.key === 'string'
            ? queueInput.key
            : undefined;
    const nameBinding = resourceBindings.name ? bindingLookup.get(resourceBindings.name) : undefined;
    const folderBinding = resourceBindings.folderPath ? bindingLookup.get(resourceBindings.folderPath) : undefined;
    const nameDefault = bindingDefault(nameBinding)
        ?? (typeof queueInput?.name === 'string' ? queueInput.name : undefined)
        ?? '';
    const folderPathDefault = bindingDefault(folderBinding)
        ?? (typeof queueInput?.folderPath === 'string' ? queueInput.folderPath : undefined)
        ?? '';
    const label = node.label ?? spec?.label ?? 'Queue';
    return {
        type: spec?.modelType ?? 'bpmn:ServiceTask',
        serviceType: resource.serviceType ?? spec?.serviceType,
        version: 'v2',
        debug: { runtime: 'bpmnEngine' },
        bindings: {
            resource: resource.resource ?? 'queue',
            ...(resourceKey ? { resourceKey } : {}),
            values: [
                {
                    name: 'name',
                    propertyAttribute: 'name',
                    default: nameDefault,
                },
                {
                    name: 'folderPath',
                    propertyAttribute: 'folderPath',
                    default: folderPathDefault,
                },
            ],
        },
        context: [
            {
                name: 'name',
                type: 'string',
                value: resourceBindings.name ? `=bindings.${resourceBindings.name}` : '<bindings.name>',
                default: nameDefault,
            },
            {
                name: 'folderPath',
                type: 'string',
                value: resourceBindings.folderPath ? `=bindings.${resourceBindings.folderPath}` : '<bindings.folderPath>',
                default: folderPathDefault,
            },
            {
                name: '_label',
                type: 'string',
                value: label,
            },
        ],
    };
}
function normalizeQueueInputs(rawInputs) {
    const out = { ...rawInputs };
    const itemData = out.itemData;
    if (itemData !== undefined && itemData !== null && typeof itemData === 'object') {
        out.itemData = JSON.stringify(itemData);
    }
    return out;
}
//# sourceMappingURL=manifest.js.map