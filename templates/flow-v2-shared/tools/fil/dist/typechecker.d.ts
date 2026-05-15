import * as AST from './ast';
export interface FunctionInfo {
    name: string;
    isAsync: boolean;
    params: AST.Parameter[];
    returnType: AST.FILType;
}
export declare class TypeChecker {
    private functions;
    private currentFunction?;
    private scopeStack;
    /**
     * Names of top-level `action` declarations. References to these in
     * `executeNode(<ident>, …)` resolve to the action and lower to a string
     * literal at WAT-emit time. References anywhere else are an error.
     */
    private actionNames;
    /** Names of top-level `trigger` declarations. Triggers cannot be invoked via executeNode. */
    private triggerNames;
    check(program: AST.Program): void;
    private checkFunction;
    private pushScope;
    private popScope;
    private defineVar;
    private lookupVar;
    private checkBlock;
    private checkStatement;
    inferExpr(expr: AST.Expression): AST.FILType;
    private inferCallType;
    private inferMethodCallType;
    getFunctions(): Map<string, FunctionInfo>;
}
//# sourceMappingURL=typechecker.d.ts.map