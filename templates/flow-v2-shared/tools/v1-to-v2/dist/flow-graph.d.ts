/**
 * Builds a directed graph from Flow nodes and edges.
 * Provides traversal utilities for the flow-to-FIL converter.
 */
import { FlowFile, NodeInstance, EdgeInstance, SubflowEntry } from './flow-types';
export interface OutEdge {
    port: string;
    targetNodeId: string;
    targetPort: string;
    edgeId: string;
}
export interface InEdge {
    port: string;
    sourceNodeId: string;
    sourcePort: string;
    edgeId: string;
}
export declare class FlowGraph {
    private nodes;
    private outgoing;
    private incoming;
    constructor(nodeList: NodeInstance[], edges: EdgeInstance[]);
    getNode(id: string): NodeInstance | undefined;
    getAllNodes(): NodeInstance[];
    getOutgoing(nodeId: string): OutEdge[];
    getIncoming(nodeId: string): InEdge[];
    /** Get the target node for a specific output port. */
    getTarget(nodeId: string, port: string): string | undefined;
    /** Get the first output target (for nodes with a single output). */
    getNextNode(nodeId: string): string | undefined;
    /** Find the start/trigger node. */
    getStartNode(): NodeInstance | undefined;
    /**
     * Find the merge/convergence node for a decision or switch node.
     * Uses BFS from each branch to find the first common downstream node.
     */
    findMergeNode(nodeId: string): string | undefined;
    /**
     * Collect all nodes reachable from `start` that come before `stopAt`.
     * Used to extract branch contents between a decision and its merge.
     */
    getNodesInBranch(start: string, stopAt: Set<string>): string[];
}
/** Build a FlowGraph from a FlowFile's top-level nodes/edges. */
export declare function buildGraph(flow: FlowFile): FlowGraph;
/** Build a FlowGraph from a subflow entry. */
export declare function buildSubflowGraph(subflow: SubflowEntry): FlowGraph;
//# sourceMappingURL=flow-graph.d.ts.map