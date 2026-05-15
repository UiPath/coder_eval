export interface CompileOptions {
    /** If true, dump the AST to stderr */
    dumpAst?: boolean;
    /** If true, dump state machine info to stderr */
    dumpSm?: boolean;
}
export type CompileStage = 'parse' | 'typecheck' | 'await-lift' | 'emit';
/**
 * Structured compile error. `line`/`col` are populated when the underlying
 * stage carries position info (currently: parser errors, since the lexer
 * tracks positions on tokens). Typechecker, await-lifter and wat-emitter
 * errors fall back to no position until AST nodes carry source locations.
 */
export interface CompileError {
    stage: CompileStage;
    message: string;
    line?: number;
    col?: number;
}
export interface CompileResult {
    wat: string;
    /**
     * Backward-compatible string view: each entry is `<Stage> error: <msg>`,
     * the same format pre-structured-errors. Existing call sites keep working.
     */
    errors: string[];
    /**
     * Structured errors with positions where available. Prefer this for
     * tooling that wants to render file:line:col or attach source context.
     */
    errorDetails: CompileError[];
    warnings: string[];
}
export declare function compile(source: string, options?: CompileOptions): CompileResult;
//# sourceMappingURL=compiler.d.ts.map