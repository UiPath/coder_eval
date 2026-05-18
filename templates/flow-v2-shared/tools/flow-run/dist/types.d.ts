export interface ManifestNode {
    /** "<nodeType>@<version>" e.g. "uipath.connector.uipath-microsoft-outlook365.send-email@1.0.0" */
    type: string;
    ui?: {
        position?: {
            x: number;
            y: number;
        };
    };
    label?: string;
    rawInputs?: Record<string, unknown>;
    /**
     * Symbolic ID of a bindings.json entry (e.g. "bOutlook"). The loader
     * resolves it to the real ConnectionId UUID via that entry's
     * `resourceKey`/`default`. Required for connector nodes.
     */
    binding?: string;
    /**
     * Symbolic ID of a bindings.json entry whose `propertyAttribute` is
     * "FolderKey". Resolved to the real FolderKey UUID the same way.
     * Required for connector nodes.
     */
    folderBinding?: string;
    /** Per-instance config values for connector nodes. */
    configuration?: Record<string, unknown>;
    inputs?: Record<string, unknown>;
    outputs?: Record<string, unknown>;
    /** Process-style resource metadata for published/in-solution process nodes. */
    resource?: ManifestResource;
    /** Maps resource property names (e.g. name/folderPath) to bindings.json IDs. */
    resourceBindings?: ManifestResourceBindings;
    /**
     * For `core.logic.mock` nodes and process-resource dispatchers in `--dry-run` mode:
     * the JSON value the dispatcher returns when the node fires. Top-level
     * (no `rawInputs.fixture` wrapper).
     */
    fixture?: unknown;
}
export interface ManifestResource {
    resource: string;
    resourceSubType?: string;
    resourceKey?: string;
    orchestratorType?: string;
    serviceType?: string;
    section?: string;
}
export interface ManifestResourceBindings {
    name?: string;
    folderPath?: string;
    [propertyAttribute: string]: string | undefined;
}
export interface FlowManifest {
    schemaVersion: string;
    flowId: string;
    flowName: string;
    flowVersion: string;
    solutionId?: string;
    projectId?: string;
    nodes: Record<string, ManifestNode>;
}
export interface BindingEntry {
    id: string;
    name: string;
    type?: string;
    resource?: string;
    resourceKey?: string;
    default?: string;
    propertyAttribute?: string;
    resourceSubType?: string;
}
export interface BindingsFile {
    schemaVersion: string;
    bindings: BindingEntry[];
}
export interface ConnectorDef {
    schemaVersion: string;
    nodeType: string;
    version: string;
    connector: {
        key: string;
        name?: string;
    };
    operation: {
        name: string;
        objectName: string;
        httpMethod?: string;
        [k: string]: unknown;
    };
    runtime?: {
        requiresConnection?: boolean;
        requiresFolderKey?: boolean;
        [k: string]: unknown;
    };
    [k: string]: unknown;
}
export interface UipConnection {
    Id: string;
    Name: string;
    ConnectorKey: string;
    ConnectorName: string;
    State: string;
    FolderKey: string;
    Folder: string;
    Owner?: string;
}
export type HistoryEvent = {
    kind: 'node';
    name: string;
    output: string;
} | {
    kind: 'timer';
    deadline: string;
} | {
    kind: 'all-marker';
    n: number;
} | {
    kind: 'race-marker';
    n: number;
    winner: number;
} | {
    kind: 'node-cancelled';
} | {
    kind: 'timer-cancelled';
};
export interface HistoryFile {
    runId: string;
    flowId: string;
    flowName: string;
    startedAt: string;
    events: HistoryEvent[];
}
export interface DecisionRecord {
    step: number;
    nodeId: string;
    nodeType: string;
    /** 'connector' | 'http' | 'mock' | 'script' | 'agent' | 'api-workflow' | 'rpa-workflow' | 'inline-agent' | 'hitl' | 'timer' */
    kind: string;
    connectorKey?: string;
    verb?: string;
    objectName?: string;
    resourceKey?: string;
    resourceSubType?: string;
    serviceType?: string;
    name?: string;
    folderPath?: string;
    source?: string;
    input: unknown;
    output: unknown;
    dispatchedAt: string;
    durationMs: number;
}
export interface DecisionsFile {
    runId: string;
    flowId: string;
    flowName: string;
    startedAt: string;
    finishedAt?: string;
    success: boolean;
    decisions: DecisionRecord[];
}
export type CrudVerb = 'create' | 'get' | 'update' | 'delete' | 'replace' | 'list';
export interface ResolvedConnectorNode {
    kind: 'connector';
    nodeId: string;
    nodeType: string;
    connectorKey: string;
    objectName: string;
    operationName: string;
    verb: CrudVerb;
    connectionId: string;
    folderKey?: string;
    connectorDefPath: string;
}
export interface ResolvedHttpNode {
    kind: 'http';
    nodeId: string;
    nodeType: string;
    /**
     * Static inputs declared in the manifest's `rawInputs` — used as defaults
     * that FIL's `executeNode` input JSON overrides per-field at dispatch time.
     * Headers and queryParams deep-merge; body and scalars are FIL-replace.
     */
    rawInputs?: Record<string, unknown>;
}
export interface ResolvedMockNode {
    kind: 'mock';
    nodeId: string;
    nodeType: string;
    /** Configured fixture (parsed JSON). Undefined → return {}. */
    fixture?: unknown;
}
export interface ResolvedScriptNode {
    kind: 'script';
    nodeId: string;
    nodeType: string;
    /** v1 `=js:` script body (e.g. `return Math.floor(Math.random() * 6) + 1;`). */
    scriptBody: string;
}
export interface ResolvedAgentNode {
    kind: 'agent';
    nodeId: string;
    nodeType: string;
    resource: string;
    resourceSubType: string;
    resourceKey?: string;
    orchestratorType?: string;
    serviceType: string;
    section?: string;
    nameBinding: string;
    folderPathBinding: string;
    name: string;
    folderPath: string;
    /** Configured dry-run fixture. Undefined -> return {}. */
    fixture?: unknown;
}
export interface ResolvedApiWorkflowNode {
    kind: 'api-workflow';
    nodeId: string;
    nodeType: string;
    resource: string;
    resourceSubType: string;
    resourceKey?: string;
    orchestratorType?: string;
    serviceType: string;
    section?: string;
    nameBinding: string;
    folderPathBinding: string;
    name: string;
    folderPath: string;
    /** Configured dry-run fixture. Undefined -> return {}. */
    fixture?: unknown;
}
export interface ResolvedRpaWorkflowNode {
    kind: 'rpa-workflow';
    nodeId: string;
    nodeType: string;
    resource: string;
    resourceSubType: string;
    resourceKey?: string;
    orchestratorType?: string;
    serviceType: string;
    section?: string;
    nameBinding: string;
    folderPathBinding: string;
    name: string;
    folderPath: string;
    /** Configured dry-run fixture. Undefined -> return {}. */
    fixture?: unknown;
}
export interface ResolvedInlineAgentNode {
    kind: 'inline-agent';
    nodeId: string;
    nodeType: string;
    source: string;
    serviceType: string;
    systemPrompt: string;
    userPrompt: string;
    model: string;
    agentInputVariables: unknown[];
    agentOutputVariables: unknown[];
    /** Configured dry-run fixture. Undefined -> return {}. */
    fixture?: unknown;
}
export interface ResolvedHitlNode {
    kind: 'hitl';
    nodeId: string;
    nodeType: string;
    taskType: 'quick';
    schema: Record<string, unknown>;
    recipient?: unknown;
    priority?: string;
    /** Configured dry-run fixture. Undefined -> return {}. */
    fixture?: unknown;
}
export type ResolvedNode = ResolvedConnectorNode | ResolvedHttpNode | ResolvedMockNode | ResolvedScriptNode | ResolvedAgentNode | ResolvedApiWorkflowNode | ResolvedRpaWorkflowNode | ResolvedInlineAgentNode | ResolvedHitlNode;
export interface PreflightReport {
    nodes: ResolvedNode[];
    warnings: string[];
    errors: string[];
}
export interface RunOptions {
    projectDir: string;
    filPath: string;
    bindingsPath?: string;
    historyPath: string;
    decisionsPath: string;
    inputJson: string;
    resume: boolean;
    dryRun: boolean;
    verbose: boolean;
    maxSteps: number;
    tenant?: string;
    libraryDir: string;
    /** When false, skip `uip is connections list` — useful for offline tests. */
    verifyConnections: boolean;
    /** When true, executeTimer decisions actually sleep (default: record the deadline immediately). */
    realTime: boolean;
}
