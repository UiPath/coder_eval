import * as AST from './ast';
import { FunctionInfo } from './typechecker';
export declare const FRAME_HEADER: {
    func_idx: number;
    state: number;
    error_code: number;
    result_ptr: number;
    exception_ptr: number;
    catch_state: number;
    finally_state: number;
    parent_frame: number;
    callee_frame: number;
    return_ptr: number;
    HEADER_SIZE: number;
};
export interface FrameSlot {
    name: string;
    offset: number;
    type: AST.FILType;
    isParam: boolean;
}
export interface AsyncFrameLayout {
    funcName: string;
    funcIdx: number;
    frameSize: number;
    slots: FrameSlot[];
}
export interface AwaitPoint {
    stateNum: number;
    awaitExpr: AST.AwaitExpression;
    resultVar?: string;
}
export interface StateMachineInfo {
    funcName: string;
    funcIdx: number;
    frameLayout: AsyncFrameLayout;
    numStates: number;
    fn: AST.FunctionDeclaration;
}
export declare class AwaitLifter {
    private asyncFunctions;
    private funcIdxMap;
    private nextFuncIdx;
    transform(program: AST.Program, functions: Map<string, FunctionInfo>): Map<string, StateMachineInfo>;
    private processAsyncFunction;
    private countAwaits;
    private walkNode;
    private findSpilledLocals;
    getStateMachines(): Map<string, StateMachineInfo>;
    getFuncIdx(name: string): number;
    getSlotOffset(info: StateMachineInfo, varName: string): number;
}
//# sourceMappingURL=await_lifter.d.ts.map