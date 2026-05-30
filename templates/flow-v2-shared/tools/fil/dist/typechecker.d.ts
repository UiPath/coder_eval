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
    /** Trigger declaration name → canonical `core.trigger.*` type. */
    private triggerTypes;
    /** Trigger declarations that are valid flow entry/start triggers. */
    private startTriggerNames;
    /** Trigger declarations that can be awaited via waitOnTrigger. */
    private eventTriggerNames;
    /** Count of `waitOnTrigger(<eventTrigger>)` call sites per event trigger. */
    private triggerWaitCounts;
    /** Nonzero while inferring the direct callee under an AwaitExpression. */
    private allowWaitOnTriggerCall;
    /**
     * Count of `executeNode(<action>, …)` call sites per declared action.
     *
     * Each `action` declaration lowers to exactly one v1 Flow node, and Flow
     * nodes cannot be reused across distinct call sites — every call site is
     * a separate position in the v1 graph with its own inputs. We therefore
     * require each action to be invoked **exactly once** lexically. Calls
     * inside a loop body or a single branch are fine; what's rejected is
     * the same identifier appearing at two different positions in the
     * source (e.g. one `executeNode(sendEmail, …)` inside the loop and a
     * second one after it).
     */
    private actionInvocationCounts;
    check(program: AST.Program): void;
    private registerTrigger;
    private validateTriggerTopology;
    private checkFunction;
    private pushScope;
    private popScope;
    private defineVar;
    private lookupVar;
    private checkBlock;
    private checkStatement;
    inferExpr(expr: AST.Expression): AST.FILType;
    private inferCallType;
    private inferWaitOnTriggerCall;
    private inferMethodCallType;
    getFunctions(): Map<string, FunctionInfo>;
}
//# sourceMappingURL=typechecker.d.ts.map