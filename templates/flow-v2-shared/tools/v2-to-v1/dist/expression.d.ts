/**
 * Phase A — classify FIL expressions and emit v1 `=js:` expression strings
 * when possible.
 *
 * The v1 BPMN engine evaluates expressions via two backends; the JS one is
 * `=js:<source>`, fed verbatim to a Jint engine. Jint supports the full
 * JavaScript language, so the constraint isn't syntax: it's
 *
 *   1. **No side effects.** The Jint engine evaluates one expression and
 *      returns a value. Assignments are blocked elsewhere in the pipeline,
 *      and any mutation we generate can't propagate to the surrounding flow.
 *      Therefore: no `=`, `++`, `--`, `await`, `new`.
 *   2. **Identifiers must resolve to context-bound names.** The engine
 *      registers flow variables under nested namespaces — `vars.foo`,
 *      `result.bar`, `outputs.x`, `iterator.i`, `metadata.*`, etc. (see
 *      BpmnExpressionNamespace.cs). A bare FIL identifier like `userId`
 *      maps to a v1 expression iff `userId` is a known flow variable.
 *
 * This module exports `tryEmitV1Expression(expr, scope)` which returns:
 *   - a `=js:`-prefixed string when the expression is fully expressible,
 *   - `null` when any sub-expression falls outside the supported subset
 *     (caller falls back to script-node emission or fails the conversion).
 *
 * Supported AST shapes (initial set — expand as needs arise):
 *   Literal, TemplateLiteral (no interpolations or all-resolvable),
 *   Identifier (when in scope), MemberExpression, ArrayExpression,
 *   ObjectExpression, BinaryExpression, LogicalExpression, UnaryExpression
 *   (`-` and `!`), ConditionalExpression, TypeAssertion (transparent),
 *   CallExpression (whitelisted: JSON.stringify, JSON.parse, Math.*,
 *   String.fromCharCode, Number.parse*, plus `.length` etc. as member access).
 *
 * NOT supported (returns null):
 *   AwaitExpression, AssignmentExpression, UpdateExpression, NewExpression,
 *   SpreadElement, SequenceExpression, arbitrary CallExpression callees,
 *   identifier references outside `scope`.
 *
 * The emitter preserves operator precedence by parenthesising aggressively —
 * Jint reparses the source so excess parens are harmless.
 */
import * as AST from 'fil-compiler/dist/ast';
/**
 * The set of names whose references the v1 engine can resolve. The string
 * value is the v1-side identifier path the FIL name maps to.
 *
 * Examples:
 *   { userId: 'vars.userId' }      // simple flow variable
 *   { metadata: 'metadata' }        // root namespace identifier
 *   { result: 'result' }           // active node-result namespace
 */
export type Scope = Map<string, string>;
export interface EmitOptions {
    /**
     * If true, the result is the JS body only — no `=js:` prefix. Useful when
     * embedding into a larger expression (e.g. recursing into ObjectExpression).
     */
    bare?: boolean;
}
export interface EmitResult {
    /** `=js:<expr>` (or `<expr>` when `bare`). */
    expression: string;
    /** Identifiers from `scope` that the expression actually referenced. */
    referenced: Set<string>;
}
/**
 * Attempt to emit the expression as a v1 expression. Returns null when any
 * sub-expression falls outside the supported subset.
 */
export declare function tryEmitV1Expression(expr: AST.Expression, scope: Scope, opts?: EmitOptions): EmitResult | null;
//# sourceMappingURL=expression.d.ts.map