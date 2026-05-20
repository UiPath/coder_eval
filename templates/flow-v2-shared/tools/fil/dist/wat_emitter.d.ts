import * as AST from './ast';
import { FunctionInfo } from './typechecker';
import { StateMachineInfo, AwaitLifter } from './await_lifter';
export declare class WatEmitter {
    private strings;
    private stringMap;
    private nextStringAddr;
    private functions;
    private stateMachines;
    private lifter;
    private asyncFuncNames;
    private usedBuiltins;
    constructor(functions: Map<string, FunctionInfo>, stateMachines: Map<string, StateMachineInfo>, lifter: AwaitLifter);
    private internString;
    private emptyStringAddr;
    private buildDataSegments;
    private byteToHex;
    static readonly SCRATCH_ADDR = 8;
    static readonly NREAD_ADDR = 16;
    static readonly STDIN_BUF = 8192;
    static readonly STDIN_SIZE = 16384;
    static readonly OUT_BUF = 24576;
    static readonly HEAP_START = 32768;
    static readonly P_EXEC_NODE = 32;
    static readonly P_EXEC_LEN = 21;
    static readonly P_INPUT = 53;
    static readonly P_INPUT_LEN = 16;
    static readonly P_END = 69;
    static readonly P_END_LEN = 2;
    static readonly P_NODE = 71;
    static readonly P_NODE_LEN = 6;
    static readonly P_OUTPUT = 77;
    static readonly P_OUTPUT_LEN = 11;
    static readonly P_FLOW_DONE = 88;
    static readonly P_FLOW_LEN = 31;
    static readonly P_TIMER_DL = 120;
    static readonly P_TIMER_DL_LEN = 26;
    static readonly P_TIMER_DUR = 146;
    static readonly P_TIMER_DUR_LEN = 26;
    static readonly P_TIMER = 172;
    static readonly P_TIMER_LEN = 7;
    static readonly P_NL = 179;
    static readonly P_NL_LEN = 1;
    static readonly P_ALL = 180;
    static readonly P_ALL_LEN = 5;
    static readonly P_RACE = 185;
    static readonly P_RACE_LEN = 6;
    static readonly P_WINNER = 191;
    static readonly P_WINNER_LEN = 8;
    static readonly P_NODE_CANC = 199;
    static readonly P_NODE_CANC_LEN = 15;
    static readonly P_TIMER_CANC = 214;
    static readonly P_TIMER_CANC_LEN = 16;
    static readonly P_NOW = 230;
    static readonly P_NOW_LEN = 5;
    static readonly P_UUID = 235;
    static readonly P_UUID_LEN = 6;
    /**
     * Map from action identifier → effective protocol id. The protocol id is
     * the action's identifier by default, or the `id` string literal from the
     * action's body when present (lets authors use kebab-case node ids without
     * having to make the FIL identifier itself dashed).
     */
    private actionIds;
    emit(program: AST.Program): string;
    private emitProtocolHelpers;
    private emitBuiltins;
    private emitDateTimeHelpers;
    private emitJsonHelpers;
    private emitSyncFunction;
    private emitAsyncInit;
    private emitAsyncResume;
    private emitStateMachineBody;
    private countAwaitExprs;
    private walkNodeShallow;
    private emitStatementAsync;
    /**
     * Whether `stmt`'s execution unconditionally exits the enclosing function
     * (via return-with-value, throw, or an exhaustive if/else where both arms
     * exit). Used by emitStatement to decide when an `if` in tail position
     * needs a `(result <T>)` clause: WebAssembly validates blocks by declared
     * signature, so we only widen the if's result type when we know all arm
     * paths produce a value (or are unreachable via throw/return).
     */
    private isValueProducingTail;
    private emitStatement;
    private emitExpr;
    private emitLiteral;
    private emitIdentifier;
    private emitTemplateLiteral;
    private emitArrayExpression;
    private emitObjectExpression;
    /**
     * Emit the WAT instruction(s) to convert the value on top of the operand
     * stack into a FIL string pointer. Used when one side of a binary `+` is
     * `string` and the other is a numeric / json value — without this, the
     * raw numeric is passed to `$str_concat` as a pointer and crashes (OOB
     * on any nonzero value).
     *
     * For `json` we reuse the tag-tracking global `$_json_tag` that's set by
     * `$json_get_field` / `$_json_parse_value` and dispatch via
     * `$_json_value_to_str`. That helper is like `$_json_stringify_value` but
     * does NOT quote STRING-tagged values (we want `"foo"` to concat as `foo`,
     * not `"foo"`).
     */
    private coerceToString;
    private emitBinaryExpression;
    private emitLogicalExpression;
    private emitUnaryExpression;
    private emitUpdateExpression;
    private emitAssignTarget;
    private emitConditionalExpression;
    private emitAssignment;
    private emitCallExpression;
    private emitMethodCall;
    private emitMemberExpression;
    private emitAwaitExpression;
    /**
     * Emit the WAT that loads the node-name string for `executeNode`'s first
     * argument. If the arg is an Identifier that resolves to a declared action,
     * substitute a string-literal load of the action's effective protocol id
     * (the action ref → string lowering). Otherwise fall through to normal
     * expression emission.
     */
    private emitActionNameArg;
    private decisionShape;
    private emitDecisionWrite;
    private emitPromiseAll;
    private emitPromiseAny;
    private emitNewExpression;
    private getLocalName;
    private getLocalRef;
    private getLocalSet;
    private watType;
    private collectLocals;
    private preScanStrings;
}
//# sourceMappingURL=wat_emitter.d.ts.map