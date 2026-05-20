/**
 * Scope-aware FIL-to-JS expression emitter for v1 `core.action.script`
 * bodies. Mirrors `v1-to-v2`'s `emitExpression` but, for `Identifier`,
 * checks the converter's `Scope` and emits the resolved v1 path
 * (`$vars.<nodeId>.<outputId>`, `$vars.<inputName>`, etc.) instead of
 * the bare FIL name.
 *
 * Background: an `async function main()` body that does
 *
 *   const rawBatch: string = await executeNode(fetchBatch, "{}");
 *   const batch: json = JSON.parse(rawBatch);
 *
 * compiles to a v1 script node whose body is the JS for the second
 * statement. The naive FIL-emitter form would be
 *
 *   return JSON.parse(rawBatch);
 *
 * but `rawBatch` is not a JS local inside that script body — the v1
 * runtime evaluator has no way to resolve it. The fix is to substitute
 * each identifier through the converter's scope, producing
 *
 *   return JSON.parse($vars.fetchBatch.output);
 *
 * which v1 evaluates correctly.
 *
 * Identifiers that aren't in scope (the JS standard library — `JSON`,
 * `Math`, `Number`, etc. — plus locals introduced inside the script
 * body itself, e.g. by `coalesceConstScriptChains`) are left verbatim.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { Scope } from './fil-to-flow';
export declare function emitScriptBodyExpression(expr: AST.Expression, scope: Scope): string;
//# sourceMappingURL=script-body-emitter.d.ts.map