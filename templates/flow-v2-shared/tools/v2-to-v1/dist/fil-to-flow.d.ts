/**
 * Converts a FIL AST (Program) back to a FlowFile.
 *
 * Strategy:
 * 1. Identify main function → top-level flow
 * 2. Other async functions → subflows
 * 3. Walk each function body generating nodes and edges:
 *    - await executeNode(name, input) → node + edge
 *    - if/else → decision + branches + merge
 *    - for...of → loop (future)
 *    - try/catch → subflow with error port (future)
 *    - return → end node
 * 4. Auto-layout positions on a grid
 */
import * as AST from 'fil-compiler/dist/ast';
import { FlowFile, FlowDefinitionFile } from 'v1-to-v2';
/**
 * FIL name → v1 expression path. E.g. `userId → 'vars.userId'` for a flow
 * global, or `item → 'iterator.item'` for a for-of iterator. Phase A's
 * v1 expression emitter consumes this to resolve identifiers.
 */
export type Scope = Map<string, string>;
export declare function filToFlow(program: AST.Program, flowId?: string, defFlow?: FlowDefinitionFile): FlowFile;
/**
 * Like `filToFlow`, but also returns the scope built for `main()`. Useful
 * when a caller wants to wire FIL expressions back into v1 expressions
 * (Phase A) using the same name-to-path mapping the converter used.
 */
export declare function filToFlowWithScope(program: AST.Program, flowId?: string, defFlow?: FlowDefinitionFile): {
    flow: FlowFile;
    scope: Scope;
};
/** Convert a FIL AST expression to a Flow expression string. */
export declare function convertFILExprToFlowExpr(expr: AST.Expression): string;
//# sourceMappingURL=fil-to-flow.d.ts.map