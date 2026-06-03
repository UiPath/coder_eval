/**
 * Converts a FlowFile to a FIL AST (Program).
 *
 * Strategy:
 * 1. Build a directed graph from Flow nodes/edges
 * 2. Walk from the start node, generating FIL statements
 * 3. Map control flow nodes to FIL constructs (if/else, for, try/catch)
 * 4. Map subflows to async helper functions
 * 5. Map variables to parameters/returns
 */
import * as AST from 'fil-compiler/dist/ast';
import { FlowFile, FlowOverrides } from './flow-types';
/**
 * Node types that flow→FIL inlines into FIL constructs:
 *   - delay   → `await executeTimer(...)`
 *   - script  → `__script_<id>` sync helper function + call site
 *
 * `core.logic.mock` is intentionally NOT inlined — it round-trips through
 * the regular `executeNode` path. A mock node is a placeholder in flow
 * authoring (often used in tests as a stand-in for an arbitrary action),
 * and dropping it would silently change graph semantics. The "stub"
 * approach preserves it as an executeNode in FIL with empty inputs.
 *
 * Inlined node types do not carry separate entries in the v2 manifest;
 * fil→flow detects the FIL constructs to round-trip back to v1 nodes.
 */
export declare const INLINED_NODE_TYPES: Set<string>;
/** Prefix for sync helper functions that represent script nodes. */
export declare const SCRIPT_FN_PREFIX = "__script_";
export declare function flowToFil(flow: FlowFile): AST.Program;
/**
 * Converts a FlowFile to FIL AST and collects per-node FlowOverrides
 * that capture metadata not representable in FIL.
 */
export declare function flowToFilWithOverrides(flow: FlowFile): {
    program: AST.Program;
    overrides: FlowOverrides;
};
/**
 * Unwrap a v1.3 source object back to its v1.0-style expression string. Accepts
 * either a bare string (v1.0 shape) or `{ type, expression, fieldType }` (v1.3
 * shape); restores the `=js:` prefix when `type === 'jsExpression'`.
 */
export declare function unwrapSource(src: unknown): string;
/**
 * Convert a Flow expression string to a FIL AST expression.
 * Flow expressions use $vars.nodeId.output syntax and =js: prefix.
 */
export declare function convertFlowExpression(expr: string, contextNodeId?: string): AST.Expression;
//# sourceMappingURL=flow-to-fil.d.ts.map