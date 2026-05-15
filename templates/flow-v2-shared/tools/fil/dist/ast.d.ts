export type FILType = 'i32' | 'i64' | 'f64' | 'bool' | 'string' | 'json' | 'DateTime' | 'TimeSpan' | 'void' | {
    kind: 'Promise';
    inner: FILType;
} | {
    kind: 'array';
    element: FILType;
} | 'unknown';
export declare function typeToString(t: FILType): string;
export declare function isPromiseType(t: FILType): t is {
    kind: 'Promise';
    inner: FILType;
};
export declare function isArrayType(t: FILType): t is {
    kind: 'array';
    element: FILType;
};
export declare function unwrapPromise(t: FILType): FILType;
export interface BaseNode {
    kind: string;
    filType?: FILType;
}
export interface Program extends BaseNode {
    kind: 'Program';
    /** Required top-level `flow <id> { name, version, … };` declaration. */
    flow?: FlowDeclaration;
    /** Top-level `action <name>: <type> { … };` declarations. */
    actions: ActionDeclaration[];
    /** Top-level `trigger <name>: <type> { … };` declarations. */
    triggers: TriggerDeclaration[];
    functions: FunctionDeclaration[];
}
/**
 * A node-type reference like `http`, `uipath.connector.uipath-atlassian-jira.create-issue`,
 * or `uipath.connector.x@1.0.0`. The name is preserved verbatim; resolution
 * to canonical v1 type strings happens downstream in v2-to-v1.
 */
export interface TypeRef extends BaseNode {
    kind: 'TypeRef';
    name: string;
    version?: string;
}
/**
 * `flow <kebab-id> { name: "…", version: "…", … };` — required at top level.
 * The identifier slot is a kebab-case flowId (allows dashes); `name` and
 * `version` are required string fields in the body.
 */
export interface FlowDeclaration extends BaseNode {
    kind: 'FlowDeclaration';
    /** Kebab-case identifier; becomes the v1 `flowId`. */
    id: string;
    /** Body fields (must include `name` and `version`). */
    fields: ObjectExpression;
}
/**
 * `action <name>: <type> { … };` — declares a node instance. The identifier
 * becomes the v1 node id; the typeRef resolves to the v1 node type.
 */
export interface ActionDeclaration extends BaseNode {
    kind: 'ActionDeclaration';
    name: string;
    typeRef: TypeRef;
    fields?: ObjectExpression;
}
/**
 * `trigger <name>: <type> { … };` — declares a trigger node. Same shape as
 * an action but lives in a separate top-level slot so converters can pick
 * out triggers without inferring from the typeRef.
 */
export interface TriggerDeclaration extends BaseNode {
    kind: 'TriggerDeclaration';
    name: string;
    typeRef: TypeRef;
    fields?: ObjectExpression;
}
export interface FunctionDeclaration extends BaseNode {
    kind: 'FunctionDeclaration';
    name: string;
    isAsync: boolean;
    params: Parameter[];
    returnType: FILType;
    body: BlockStatement;
}
export interface Parameter {
    name: string;
    type: FILType;
}
export interface BlockStatement extends BaseNode {
    kind: 'BlockStatement';
    body: Statement[];
}
export interface VariableDeclaration extends BaseNode {
    kind: 'VariableDeclaration';
    declarationKind: 'let' | 'const';
    name: string;
    typeAnnotation?: FILType;
    initializer?: Expression;
}
export interface ExpressionStatement extends BaseNode {
    kind: 'ExpressionStatement';
    expression: Expression;
}
export interface ReturnStatement extends BaseNode {
    kind: 'ReturnStatement';
    argument?: Expression;
}
export interface ThrowStatement extends BaseNode {
    kind: 'ThrowStatement';
    argument: Expression;
}
export interface IfStatement extends BaseNode {
    kind: 'IfStatement';
    test: Expression;
    consequent: Statement;
    alternate?: Statement;
}
export interface WhileStatement extends BaseNode {
    kind: 'WhileStatement';
    label?: string;
    test: Expression;
    body: Statement;
}
export interface ForStatement extends BaseNode {
    kind: 'ForStatement';
    label?: string;
    init?: VariableDeclaration | ExpressionStatement;
    test?: Expression;
    update?: Expression;
    body: Statement;
}
export interface ForOfStatement extends BaseNode {
    kind: 'ForOfStatement';
    label?: string;
    declarationKind: 'let' | 'const';
    item: string;
    iterable: Expression;
    body: Statement;
}
export interface ForInStatement extends BaseNode {
    kind: 'ForInStatement';
    label?: string;
    declarationKind: 'let' | 'const';
    key: string;
    object: Expression;
    body: Statement;
}
export interface SwitchStatement extends BaseNode {
    kind: 'SwitchStatement';
    discriminant: Expression;
    cases: SwitchCase[];
}
export interface SwitchCase extends BaseNode {
    kind: 'SwitchCase';
    test?: Expression;
    consequent: Statement[];
}
export interface TryStatement extends BaseNode {
    kind: 'TryStatement';
    block: BlockStatement;
    handler?: CatchClause;
    finalizer?: BlockStatement;
}
export interface CatchClause extends BaseNode {
    kind: 'CatchClause';
    param?: string;
    body: BlockStatement;
}
export interface BreakStatement extends BaseNode {
    kind: 'BreakStatement';
    label?: string;
}
export interface ContinueStatement extends BaseNode {
    kind: 'ContinueStatement';
    label?: string;
}
export interface LabeledStatement extends BaseNode {
    kind: 'LabeledStatement';
    label: string;
    body: Statement;
}
export type Statement = BlockStatement | VariableDeclaration | ExpressionStatement | ReturnStatement | ThrowStatement | IfStatement | WhileStatement | ForStatement | ForOfStatement | ForInStatement | SwitchStatement | TryStatement | BreakStatement | ContinueStatement | LabeledStatement;
export interface Identifier extends BaseNode {
    kind: 'Identifier';
    name: string;
}
export interface Literal extends BaseNode {
    kind: 'Literal';
    value: string | number | boolean | null;
    raw: string;
}
export interface TemplateLiteral extends BaseNode {
    kind: 'TemplateLiteral';
    quasis: string[];
    expressions: Expression[];
}
export interface ArrayExpression extends BaseNode {
    kind: 'ArrayExpression';
    elements: Expression[];
}
export interface ObjectExpression extends BaseNode {
    kind: 'ObjectExpression';
    properties: ObjectProperty[];
}
export interface ObjectProperty extends BaseNode {
    kind: 'ObjectProperty';
    key: string;
    value: Expression;
    shorthand: boolean;
}
export interface SpreadElement extends BaseNode {
    kind: 'SpreadElement';
    argument: Expression;
}
export interface AssignmentExpression extends BaseNode {
    kind: 'AssignmentExpression';
    operator: string;
    left: Expression;
    right: Expression;
}
export interface BinaryExpression extends BaseNode {
    kind: 'BinaryExpression';
    operator: string;
    left: Expression;
    right: Expression;
}
export interface LogicalExpression extends BaseNode {
    kind: 'LogicalExpression';
    operator: '&&' | '||' | '??';
    left: Expression;
    right: Expression;
}
export interface UnaryExpression extends BaseNode {
    kind: 'UnaryExpression';
    operator: string;
    argument: Expression;
    prefix: boolean;
}
export interface UpdateExpression extends BaseNode {
    kind: 'UpdateExpression';
    operator: '++' | '--';
    argument: Expression;
    prefix: boolean;
}
export interface ConditionalExpression extends BaseNode {
    kind: 'ConditionalExpression';
    test: Expression;
    consequent: Expression;
    alternate: Expression;
}
export interface CallExpression extends BaseNode {
    kind: 'CallExpression';
    callee: Expression;
    arguments: Expression[];
}
export interface MemberExpression extends BaseNode {
    kind: 'MemberExpression';
    object: Expression;
    property: Expression;
    computed: boolean;
    optional: boolean;
}
export interface AwaitExpression extends BaseNode {
    kind: 'AwaitExpression';
    argument: Expression;
}
export interface NewExpression extends BaseNode {
    kind: 'NewExpression';
    callee: Expression;
    arguments: Expression[];
}
export interface TypeAssertion extends BaseNode {
    kind: 'TypeAssertion';
    expression: Expression;
    assertedType: FILType;
}
export interface SequenceExpression extends BaseNode {
    kind: 'SequenceExpression';
    expressions: Expression[];
}
export type Expression = Identifier | Literal | TemplateLiteral | ArrayExpression | ObjectExpression | SpreadElement | AssignmentExpression | BinaryExpression | LogicalExpression | UnaryExpression | UpdateExpression | ConditionalExpression | CallExpression | MemberExpression | AwaitExpression | NewExpression | TypeAssertion | SequenceExpression;
export declare function isExpression(node: BaseNode): node is Expression;
export declare function isStatement(node: BaseNode): node is Statement;
//# sourceMappingURL=ast.d.ts.map