/**
 * Pretty-prints a FIL AST back to valid FIL/TypeScript source code.
 */
import * as AST from 'fil-compiler/dist/ast';
export declare function emitProgram(program: AST.Program): string;
export declare function emitType(t: AST.FILType): string;
/**
 * Emit a single FIL expression as TypeScript/JS source. Public for
 * callers that need to embed a FIL expression inside another shape (e.g.
 * a v1 `core.action.script` body's `return <expr>;`).
 */
export declare function emitExpression(expr: AST.Expression): string;
//# sourceMappingURL=fil-emitter.d.ts.map