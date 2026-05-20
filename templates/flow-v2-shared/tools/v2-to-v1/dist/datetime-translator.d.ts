/**
 * FIL DateTime/TimeSpan → v1 JS Date translation.
 *
 * FIL has first-class `DateTime` and `TimeSpan` types with a known method
 * surface (see fil/src/typechecker.ts §"DateTime instance methods"). The
 * v1 .flow evaluator (Jint) is plain JS and has no equivalent — both
 * types lower to JS `number` (ms timestamp / ms duration), with method
 * calls rewritten to `Date`/arithmetic expressions.
 *
 * Used by `script-body-emitter` and `convertFILExprToFlowExpr` to
 * rewrite known DateTime/TimeSpan call sites at emit time. Returns
 * `null` when the call doesn't match a known DateTime/TimeSpan pattern,
 * letting the caller fall through to its normal handling.
 */
import * as AST from 'fil-compiler/dist/ast';
/** Emitter callback the translator uses to recursively render argument expressions. */
export type EmitFn = (e: AST.Expression) => string;
/**
 * If `expr` is a CallExpression matching a known DateTime/TimeSpan
 * static or instance method, returns the v1 JS equivalent as a string.
 * Returns `null` otherwise.
 */
export declare function tryTranslateDateTimeCall(expr: AST.CallExpression, emit: EmitFn): string | null;
//# sourceMappingURL=datetime-translator.d.ts.map