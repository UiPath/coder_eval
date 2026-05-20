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
import { FlowFile, FlowOverrides } from 'v1-to-v2';
/**
 * FIL name → v1 expression path. E.g. `userId → 'vars.userId'` for a flow
 * global, or `item → 'iterator.item'` for a for-of iterator. Phase A's
 * v1 expression emitter consumes this to resolve identifiers.
 */
export type Scope = Map<string, string>;
export declare function filToFlow(program: AST.Program, flowId?: string, overrides?: FlowOverrides): FlowFile;
/**
 * Like `filToFlow`, but also returns the scope built for `main()`. Useful
 * when a caller wants to wire FIL expressions back into v1 expressions
 * (Phase A) using the same name-to-path mapping the converter used.
 */
export declare function filToFlowWithScope(program: AST.Program, flowId?: string, overrides?: FlowOverrides): {
    flow: FlowFile;
    scope: Scope;
};
/** Convert a FIL AST expression to a Flow expression string. */
/**
 * Convert a FIL AST expression into a v1 flow expression string (the
 * subset of JS the v1 Jint evaluator accepts for non-script value
 * fields: decision conditions, loop collections, end-node output
 * sources, etc.).
 *
 * If `scope` is provided, identifiers are looked up against it and
 * resolved to the v1 path (`$vars.X`, `$vars.<loopId>.currentItem`,
 * inline-alias expressions, etc.). Without a scope, bare identifiers
 * pass through verbatim — the legacy behavior, used only by call
 * sites that already pre-substitute via the script-body-emitter.
 */
export declare function convertFILExprToFlowExpr(expr: AST.Expression, scope?: Scope): string;
/**
 * Strip the patch component from every `typeVersion` / definition `version`
 * string. The v2-to-v1 emitter hardcodes `1.0.0` at every node-mint site,
 * but the Studio Web Flow editor (VS Code extension) flags `1.0.0` and
 * expects `1.0` instead. Until the version-pinning story is sorted out
 * upstream, this post-pass normalizes everything we emit to `MAJOR.MINOR`.
 *
 * Only touches `typeVersion` on node instances and `version` on
 * definitions — leaves the flow-level `version` (the user-facing flow
 * version string) and any other `version` field untouched.
 *
 * Called both at the end of `filToFlow` (for direct callers like tests
 * and cs2fil) and at the end of `convertV2ToV1` after definitions are
 * rebuilt from the canonical library.
 */
export declare function normalizeTypeVersions(flow: FlowFile): void;
//# sourceMappingURL=fil-to-flow.d.ts.map