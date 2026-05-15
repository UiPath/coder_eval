"use strict";
/**
 * Builds a directed graph from Flow nodes and edges.
 * Provides traversal utilities for the flow-to-FIL converter.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.FlowGraph = void 0;
exports.buildGraph = buildGraph;
exports.buildSubflowGraph = buildSubflowGraph;
const flow_types_1 = require("./flow-types");
class FlowGraph {
    constructor(nodeList, edges) {
        this.nodes = new Map();
        this.outgoing = new Map();
        this.incoming = new Map();
        for (const node of nodeList) {
            this.nodes.set(node.id, node);
            this.outgoing.set(node.id, []);
            this.incoming.set(node.id, []);
        }
        for (const edge of edges) {
            this.outgoing.get(edge.sourceNodeId)?.push({
                port: edge.sourcePort,
                targetNodeId: edge.targetNodeId,
                targetPort: edge.targetPort,
                edgeId: edge.id,
            });
            this.incoming.get(edge.targetNodeId)?.push({
                port: edge.targetPort,
                sourceNodeId: edge.sourceNodeId,
                sourcePort: edge.sourcePort,
                edgeId: edge.id,
            });
        }
    }
    getNode(id) {
        return this.nodes.get(id);
    }
    getAllNodes() {
        return Array.from(this.nodes.values());
    }
    getOutgoing(nodeId) {
        return this.outgoing.get(nodeId) || [];
    }
    getIncoming(nodeId) {
        return this.incoming.get(nodeId) || [];
    }
    /** Get the target node for a specific output port. */
    getTarget(nodeId, port) {
        const edge = this.getOutgoing(nodeId).find(e => e.port === port);
        return edge?.targetNodeId;
    }
    /** Get the first output target (for nodes with a single output). */
    getNextNode(nodeId) {
        const edges = this.getOutgoing(nodeId);
        if (edges.length === 0)
            return undefined;
        // Prefer 'output' port, then 'success', then first available
        const byPort = edges.find(e => e.port === 'output')
            || edges.find(e => e.port === 'success')
            || edges[0];
        return byPort.targetNodeId;
    }
    /** Find the start/trigger node. */
    getStartNode() {
        for (const node of this.nodes.values()) {
            if ((0, flow_types_1.isTriggerNode)(node.type))
                return node;
        }
        // Fallback: node with no incoming edges
        for (const node of this.nodes.values()) {
            if ((this.incoming.get(node.id) || []).length === 0)
                return node;
        }
        return undefined;
    }
    /**
     * Find the merge/convergence node for a decision or switch node.
     * Uses BFS from each branch to find the first common downstream node.
     */
    findMergeNode(nodeId) {
        const outEdges = this.getOutgoing(nodeId);
        if (outEdges.length < 2)
            return undefined;
        // BFS from each branch, collect reachable nodes in visit order
        const branchReachable = [];
        const firstBranchOrder = [];
        for (let i = 0; i < outEdges.length; i++) {
            const reachable = new Set();
            const queue = [outEdges[i].targetNodeId];
            const visited = new Set();
            while (queue.length > 0) {
                const cur = queue.shift();
                if (visited.has(cur))
                    continue;
                visited.add(cur);
                reachable.add(cur);
                if (i === 0)
                    firstBranchOrder.push(cur);
                for (const out of this.getOutgoing(cur)) {
                    if (!visited.has(out.targetNodeId)) {
                        queue.push(out.targetNodeId);
                    }
                }
            }
            branchReachable.push(reachable);
        }
        // Find the first node from branch 0 that appears in ALL other branches
        for (const nodeId of firstBranchOrder) {
            if (branchReachable.every(set => set.has(nodeId))) {
                return nodeId;
            }
        }
        return undefined;
    }
    /**
     * Collect all nodes reachable from `start` that come before `stopAt`.
     * Used to extract branch contents between a decision and its merge.
     */
    getNodesInBranch(start, stopAt) {
        const result = [];
        const visited = new Set();
        const queue = [start];
        while (queue.length > 0) {
            const cur = queue.shift();
            if (visited.has(cur) || stopAt.has(cur))
                continue;
            visited.add(cur);
            result.push(cur);
            for (const out of this.getOutgoing(cur)) {
                if (!visited.has(out.targetNodeId) && !stopAt.has(out.targetNodeId)) {
                    queue.push(out.targetNodeId);
                }
            }
        }
        return result;
    }
}
exports.FlowGraph = FlowGraph;
/** Build a FlowGraph from a FlowFile's top-level nodes/edges. */
function buildGraph(flow) {
    return new FlowGraph(flow.nodes, flow.edges);
}
/** Build a FlowGraph from a subflow entry. */
function buildSubflowGraph(subflow) {
    return new FlowGraph(subflow.nodes, subflow.edges);
}
//# sourceMappingURL=flow-graph.js.map