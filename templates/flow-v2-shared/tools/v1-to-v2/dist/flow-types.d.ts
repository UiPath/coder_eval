/**
 * TypeScript interfaces for the .flow JSON format.
 * These cover the subset of the format relevant to flow2fil conversion.
 */
export interface FlowFile {
    id: string;
    version: string;
    name: string;
    description?: string;
    nodes: NodeInstance[];
    edges: EdgeInstance[];
    definitions?: NodeDefinition[];
    variables?: WorkflowVariables;
    subflows?: Record<string, SubflowEntry>;
    metadata?: Metadata;
    bindings?: unknown[];
    solutionId?: string;
    projectId?: string;
}
export interface NodeInstance {
    id: string;
    type: string;
    typeVersion: string;
    ui: NodeUI;
    display: NodeDisplay;
    inputs?: Record<string, unknown>;
    outputs?: Record<string, {
        source: string;
    }>;
    model?: Record<string, unknown>;
    uipath?: Record<string, unknown>;
    parentId?: string;
}
export interface NodeUI {
    position: {
        x: number;
        y: number;
    };
    size?: {
        width: number;
        height: number;
    };
    collapsed?: boolean;
}
export interface NodeDisplay {
    label: string;
    subLabel?: string;
    icon?: string;
    shape?: string;
    iconBackground?: string;
    iconBackgroundDark?: string;
}
export interface EdgeInstance {
    id: string;
    sourceNodeId: string;
    sourcePort: string;
    targetNodeId: string;
    targetPort: string;
}
export interface NodeDefinition {
    nodeType: string;
    version: string;
    category: string;
    tags?: string[];
    sortOrder?: number;
    supportsErrorHandling?: boolean;
    display: NodeDisplay;
    handleConfiguration?: HandleGroup[];
    model?: Record<string, unknown>;
    inputDefinition?: Record<string, unknown>;
    inputDefaults?: Record<string, unknown>;
    outputDefinition?: Record<string, unknown>;
    form?: Record<string, unknown>;
}
export interface HandleGroup {
    position: string;
    handles: HandleConfig[];
    visible?: boolean;
}
export interface HandleConfig {
    id: string;
    type: string;
    handleType: string;
    label?: string;
    visible?: string;
    showButton?: boolean;
    isDefaultForType?: boolean;
    constraints?: Record<string, unknown>;
}
export interface WorkflowVariables {
    globals?: GlobalVariable[];
    nodes?: NodeVariable[];
    variableUpdates?: Record<string, VariableUpdate[]>;
}
export interface GlobalVariable {
    id: string;
    name?: string;
    direction: 'in' | 'out' | 'inout';
    type: string;
    subType?: string;
    defaultValue?: unknown;
    description?: string;
    triggerNodeId?: string;
}
export interface NodeVariable {
    id: string;
    name?: string;
    type: string;
    description?: string;
    binding: {
        nodeId: string;
        outputId: string;
    };
    schema?: unknown;
}
export interface VariableUpdate {
    variableId: string;
    expression: string;
}
export interface SubflowEntry {
    nodes: NodeInstance[];
    edges: EdgeInstance[];
    variables?: WorkflowVariables;
}
export interface Metadata {
    createdAt: string;
    updatedAt: string;
    author?: string;
    tags?: string[];
    description?: string;
}
/**
 * In-memory bundle of everything a v1 .flow file carries that FIL does not
 * express directly: flow header, embedded type definitions, bindings, and
 * per-node metadata. Built during conversion from a parsed FIL + library +
 * bindings and consumed by `filToFlow` to reconstruct a complete .flow.
 */
export interface FlowOverrides {
    flowId: string;
    flowName: string;
    flowVersion: string;
    solutionId?: string;
    projectId?: string;
    metadata?: Metadata;
    definitions: NodeDefinition[];
    bindings: unknown[];
    nodeOverrides: Record<string, NodeOverride>;
    variableOverrides?: WorkflowVariables;
}
/**
 * Per-node metadata that supplements what FIL can express.
 * Keyed by node ID in the FlowOverrides.nodeOverrides map.
 */
export interface NodeOverride {
    /** The node type (e.g., "uipath.connector.uipath-salesforce-slack.send-message-to-user") */
    type?: string;
    typeVersion?: string;
    display?: Partial<NodeDisplay>;
    ui?: Partial<NodeUI>;
    model?: Record<string, unknown>;
    inputsDetail?: Record<string, unknown>;
    /** Full inputs object from the original flow node */
    inputs?: Record<string, unknown>;
    /** Full outputs object from the original flow node */
    outputs?: Record<string, {
        source: string;
    }>;
}
export declare const NODE_TYPES: {
    readonly TRIGGER_MANUAL: "core.trigger.manual";
    readonly TRIGGER_SCHEDULED: "core.trigger.scheduled";
    readonly DECISION: "core.logic.decision";
    readonly SWITCH: "core.logic.switch";
    readonly MERGE: "core.logic.merge";
    readonly LOOP: "core.logic.loop";
    readonly DELAY: "core.logic.delay";
    readonly MOCK: "core.logic.mock";
    readonly TERMINATE: "core.logic.terminate";
    readonly END: "core.control.end";
    readonly SCRIPT: "core.action.script";
    readonly HTTP: "core.action.http";
    readonly HTTP_V2: "core.action.http.v2";
    readonly SUBFLOW: "core.subflow";
};
export declare function isTriggerNode(type: string): boolean;
export declare function isControlFlowNode(type: string): boolean;
//# sourceMappingURL=flow-types.d.ts.map