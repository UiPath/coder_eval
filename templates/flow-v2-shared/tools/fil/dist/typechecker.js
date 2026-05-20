"use strict";
// Type checker and inference for the FIL compiler
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.TypeChecker = void 0;
const AST = __importStar(require("./ast"));
/**
 * Reject non-deterministic JS APIs inside a `__script_<id>` helper body.
 *
 * Script helpers run on every FIL replay; if a body's return value depends
 * on entropy (`Math.random()`), wall-clock time (`Date.now()`, `new Date()`),
 * or other environment state, replays drift and the position-keyed history
 * no longer matches what the WASM emits.
 *
 * The replay-safe pattern is to invoke the script via `await executeNode`
 * so the host runs it once and captures the result in history.
 */
const NON_DETERMINISTIC_MEMBERS = {
    Math: new Set(['random']),
    Date: new Set(['now']),
    crypto: new Set(['getRandomValues', 'randomUUID', 'randomBytes', 'randomInt']),
    performance: new Set(['now']),
};
// Bare-call non-deterministic globals: `Date()` returns the current time as
// a string. Both `Date()` and `new Date()` (no args) hit this.
const NON_DETERMINISTIC_BARE_CALLS = new Set(['Date']);
function checkScriptHelperDeterminism(fn) {
    const errors = [];
    const visit = (node) => {
        if (!node)
            return;
        // Catch `Math.random()`, `Date.now()`, `crypto.randomUUID()`,
        // and bare-global calls like `Date()`.
        if (node.kind === 'CallExpression') {
            const ce = node;
            const memberName = nonDeterministicMemberName(ce.callee);
            if (memberName) {
                errors.push(`${memberName}() is non-deterministic. In a __script_<id> helper this ` +
                    `runs on every FIL replay and breaks history-replay invariants. ` +
                    `Move the call into a script node invoked via ` +
                    `\`await executeNode("nodeName", ...)\` so the result is captured ` +
                    `in history (see SKILL.md → "Non-deterministic calls must go ` +
                    `through executeNode").`);
            }
            else if (ce.callee.kind === 'Identifier' &&
                NON_DETERMINISTIC_BARE_CALLS.has(ce.callee.name) &&
                ce.arguments.length === 0) {
                const id = ce.callee.name;
                errors.push(`${id}() with no arguments is non-deterministic (returns the ` +
                    `current time). In a __script_<id> helper this breaks replay ` +
                    `determinism. Move the call into a script node invoked via ` +
                    `\`await executeNode(...)\`.`);
            }
            visit(ce.callee);
            for (const a of ce.arguments)
                visit(a);
            return;
        }
        // Catch `new Date()` (no args → current time). The parser nests it as
        // NewExpression { callee: CallExpression { callee: Identifier(Date) } },
        // which the CallExpression branch above will also catch — but flag the
        // outer form too in case the parser shape ever changes.
        if (node.kind === 'NewExpression') {
            const ne = node;
            if (ne.callee.kind === 'Identifier' &&
                NON_DETERMINISTIC_BARE_CALLS.has(ne.callee.name) &&
                ne.arguments.length === 0) {
                const id = ne.callee.name;
                errors.push(`\`new ${id}()\` is non-deterministic (returns the current time). ` +
                    `In a __script_<id> helper this breaks replay determinism. ` +
                    `Move the call into a script node invoked via ` +
                    `\`await executeNode(...)\`.`);
            }
            visit(ne.callee);
            for (const a of ne.arguments)
                visit(a);
            return;
        }
        visitChildren(node, visit);
    };
    visit(fn.body);
    if (errors.length > 0) {
        throw new Error(errors.join('\n'));
    }
}
/** Pull the dotted name out of a member expression callee, if it matches our denylist. */
function nonDeterministicMemberName(callee) {
    if (callee.kind !== 'MemberExpression')
        return null;
    const me = callee;
    if (me.computed)
        return null;
    if (me.object.kind !== 'Identifier' || me.property.kind !== 'Identifier')
        return null;
    const obj = me.object.name;
    const prop = me.property.name;
    const props = NON_DETERMINISTIC_MEMBERS[obj];
    return props && props.has(prop) ? `${obj}.${prop}` : null;
}
/** Recursive visitor — covers every expression child for the determinism scan. */
function visitChildren(node, visit) {
    switch (node.kind) {
        case 'BlockStatement':
            for (const s of node.body)
                visit(s);
            return;
        case 'VariableDeclaration': {
            const v = node;
            if (v.initializer)
                visit(v.initializer);
            return;
        }
        case 'ExpressionStatement':
            visit(node.expression);
            return;
        case 'ReturnStatement': {
            const r = node;
            if (r.argument)
                visit(r.argument);
            return;
        }
        case 'ThrowStatement':
            visit(node.argument);
            return;
        case 'IfStatement': {
            const i = node;
            visit(i.test);
            visit(i.consequent);
            if (i.alternate)
                visit(i.alternate);
            return;
        }
        case 'WhileStatement': {
            const w = node;
            visit(w.test);
            visit(w.body);
            return;
        }
        case 'ForStatement': {
            const f = node;
            if (f.init)
                visit(f.init);
            if (f.test)
                visit(f.test);
            if (f.update)
                visit(f.update);
            visit(f.body);
            return;
        }
        case 'ForOfStatement': {
            const f = node;
            visit(f.iterable);
            visit(f.body);
            return;
        }
        case 'SwitchStatement': {
            const s = node;
            visit(s.discriminant);
            for (const c of s.cases) {
                if (c.test)
                    visit(c.test);
                for (const st of c.consequent)
                    visit(st);
            }
            return;
        }
        case 'TryStatement': {
            const t = node;
            visit(t.block);
            if (t.handler)
                visit(t.handler.body);
            if (t.finalizer)
                visit(t.finalizer);
            return;
        }
        case 'BinaryExpression': {
            const b = node;
            visit(b.left);
            visit(b.right);
            return;
        }
        case 'LogicalExpression': {
            const l = node;
            visit(l.left);
            visit(l.right);
            return;
        }
        case 'UnaryExpression':
            visit(node.argument);
            return;
        case 'UpdateExpression':
            visit(node.argument);
            return;
        case 'ConditionalExpression': {
            const c = node;
            visit(c.test);
            visit(c.consequent);
            visit(c.alternate);
            return;
        }
        case 'AssignmentExpression': {
            const a = node;
            visit(a.left);
            visit(a.right);
            return;
        }
        case 'MemberExpression': {
            const m = node;
            visit(m.object);
            if (m.computed)
                visit(m.property);
            return;
        }
        case 'ArrayExpression': {
            const a = node;
            for (const e of a.elements)
                if (e)
                    visit(e);
            return;
        }
        case 'ObjectExpression': {
            const o = node;
            for (const p of o.properties)
                visit(p.value);
            return;
        }
        case 'TemplateLiteral': {
            const t = node;
            for (const e of t.expressions)
                visit(e);
            return;
        }
        case 'AwaitExpression':
            visit(node.argument);
            return;
        // Literal, Identifier — nothing to recurse into.
    }
}
/**
 * Contextual typing for numeric literals.
 *
 * JS numbers don't distinguish `0` from `0.0` at the value level, so
 * the parser can't tell from the AST whether the author meant an integer
 * or a float. When a literal appears in a context with a known target
 * type (e.g. `let a: f64 = 0`, `function f(): f64 { return 0; }`), pin
 * the literal's `filType` to that target so the WAT emitter picks the
 * right `<type>.const` instruction instead of the value-shape heuristic.
 */
function coerceNumericLiteral(expr, target) {
    if (typeof target !== 'string')
        return;
    if (target !== 'f64' && target !== 'i32' && target !== 'i64')
        return;
    if (expr.kind !== 'Literal')
        return;
    const lit = expr;
    if (typeof lit.value !== 'number')
        return;
    // i64 literals were tagged by the parser via the BigInt suffix `n`;
    // don't second-guess that.
    if (lit.filType === 'i64' && target !== 'i64')
        return;
    lit.filType = target;
}
class TypeChecker {
    constructor() {
        this.functions = new Map();
        this.scopeStack = [];
        /**
         * Names of top-level `action` declarations. References to these in
         * `executeNode(<ident>, …)` resolve to the action and lower to a string
         * literal at WAT-emit time. References anywhere else are an error.
         */
        this.actionNames = new Set();
        /** Names of top-level `trigger` declarations. Triggers cannot be invoked via executeNode. */
        this.triggerNames = new Set();
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
        this.actionInvocationCounts = new Map();
    }
    check(program) {
        // Register top-level node declarations first.
        for (const a of program.actions)
            this.actionNames.add(a.name);
        for (const t of program.triggers)
            this.triggerNames.add(t.name);
        // First pass: register all functions
        for (const fn of program.functions) {
            this.functions.set(fn.name, {
                name: fn.name,
                isAsync: fn.isAsync,
                params: fn.params,
                returnType: fn.returnType,
            });
        }
        // Validate main exists
        if (!this.functions.has('main')) {
            throw new Error('FIL program must define a main() function');
        }
        const main = this.functions.get('main');
        if (main.params.length > 0) {
            throw new Error('main() must take no parameters');
        }
        // Second pass: type check each function
        for (const fn of program.functions) {
            this.checkFunction(fn);
        }
        // Third pass: validate that every declared action is invoked exactly
        // once via executeNode. Actions lower to v1 Flow nodes 1:1, so a name
        // used at two call sites would collapse to a single shared node — but
        // Flow nodes can't be shared across call sites (each holds its own
        // inputs and graph position). Likewise an action with zero call sites
        // is dead code that would still synthesize a stub node in the v1
        // output.
        for (const action of program.actions) {
            const count = this.actionInvocationCounts.get(action.name) ?? 0;
            if (count === 0) {
                throw new Error(`action "${action.name}" is declared but never invoked via executeNode. ` +
                    `Either remove the declaration or add an \`await executeNode(${action.name}, …)\` call.`);
            }
            if (count > 1) {
                throw new Error(`action "${action.name}" is invoked ${count} times via executeNode, but each ` +
                    `action lowers to exactly one v1 Flow node — nodes can't be reused across ` +
                    `distinct call sites. Declare a separate action per call site (e.g., ` +
                    `"${action.name}1", "${action.name}2", …) with its own inputs/bindings, ` +
                    `or refactor so the action is only called from one position.`);
            }
        }
    }
    checkFunction(fn) {
        const info = this.functions.get(fn.name);
        this.currentFunction = info;
        this.pushScope();
        for (const p of fn.params) {
            this.defineVar(p.name, p.type);
        }
        // Sync helpers prefixed with __script_ become v1 script nodes when
        // converted (their bodies are extracted as text). They also get
        // inlined into the FIL state machine and execute on every WASM
        // replay — non-deterministic JS APIs in the body break replay
        // determinism. Reject the common offenders with an actionable hint.
        if (!fn.isAsync && fn.name.startsWith('__script_')) {
            checkScriptHelperDeterminism(fn);
        }
        this.checkBlock(fn.body);
        this.popScope();
        this.currentFunction = undefined;
    }
    pushScope() {
        this.scopeStack.push(new Map());
    }
    popScope() {
        this.scopeStack.pop();
    }
    defineVar(name, type) {
        const top = this.scopeStack[this.scopeStack.length - 1];
        top.set(name, type);
    }
    lookupVar(name) {
        for (let i = this.scopeStack.length - 1; i >= 0; i--) {
            const t = this.scopeStack[i].get(name);
            if (t !== undefined)
                return t;
        }
        return undefined;
    }
    checkBlock(block) {
        this.pushScope();
        for (const stmt of block.body) {
            this.checkStatement(stmt);
        }
        this.popScope();
    }
    checkStatement(stmt) {
        switch (stmt.kind) {
            case 'BlockStatement':
                this.checkBlock(stmt);
                break;
            case 'VariableDeclaration': {
                let type = 'unknown';
                if (stmt.initializer) {
                    type = this.inferExpr(stmt.initializer);
                }
                if (stmt.typeAnnotation) {
                    // Validate annotation if we can
                    type = stmt.typeAnnotation;
                    // Contextual typing for numeric literals: if the user wrote
                    // `let a: f64 = 0` (or `0.0`), the literal value loses the
                    // distinction between integer and float at the JS-number level.
                    // Coerce the initializer's filType to match the annotation so
                    // the WAT emitter picks `f64.const` instead of `i32.const`.
                    if (stmt.initializer)
                        coerceNumericLiteral(stmt.initializer, type);
                }
                else if (type === 'unknown') {
                    type = 'json';
                }
                stmt.filType = type;
                this.defineVar(stmt.name, type);
                break;
            }
            case 'ExpressionStatement':
                this.inferExpr(stmt.expression);
                break;
            case 'ReturnStatement':
                if (stmt.argument) {
                    // Coerce return-statement numeric literals against the
                    // declared return type for the same reason as variable
                    // initializers (e.g. `function f(): f64 { return 0; }`).
                    if (this.currentFunction) {
                        const declared = AST.unwrapPromise(this.currentFunction.returnType);
                        coerceNumericLiteral(stmt.argument, declared);
                    }
                    this.inferExpr(stmt.argument);
                }
                break;
            case 'ThrowStatement':
                this.inferExpr(stmt.argument);
                break;
            case 'IfStatement':
                this.inferExpr(stmt.test);
                this.checkStatement(stmt.consequent);
                if (stmt.alternate)
                    this.checkStatement(stmt.alternate);
                break;
            case 'WhileStatement':
                this.inferExpr(stmt.test);
                this.checkStatement(stmt.body);
                break;
            case 'ForStatement': {
                this.pushScope();
                if (stmt.init)
                    this.checkStatement(stmt.init);
                if (stmt.test)
                    this.inferExpr(stmt.test);
                if (stmt.update)
                    this.inferExpr(stmt.update);
                this.checkStatement(stmt.body);
                this.popScope();
                break;
            }
            case 'ForOfStatement': {
                const iterType = this.inferExpr(stmt.iterable);
                let elemType = 'json';
                if (AST.isArrayType(iterType)) {
                    elemType = iterType.element;
                }
                this.pushScope();
                this.defineVar(stmt.item, elemType);
                this.checkStatement(stmt.body);
                this.popScope();
                break;
            }
            case 'ForInStatement': {
                this.inferExpr(stmt.object);
                this.pushScope();
                this.defineVar(stmt.key, 'string');
                this.checkStatement(stmt.body);
                this.popScope();
                break;
            }
            case 'SwitchStatement': {
                this.inferExpr(stmt.discriminant);
                for (const c of stmt.cases) {
                    if (c.test)
                        this.inferExpr(c.test);
                    this.pushScope();
                    for (const s of c.consequent)
                        this.checkStatement(s);
                    this.popScope();
                }
                break;
            }
            case 'TryStatement': {
                this.checkBlock(stmt.block);
                if (stmt.handler) {
                    this.pushScope();
                    if (stmt.handler.param) {
                        this.defineVar(stmt.handler.param, 'json');
                    }
                    this.checkBlock(stmt.handler.body);
                    this.popScope();
                }
                if (stmt.finalizer)
                    this.checkBlock(stmt.finalizer);
                break;
            }
            case 'BreakStatement':
            case 'ContinueStatement':
                break;
            case 'LabeledStatement':
                this.checkStatement(stmt.body);
                break;
        }
    }
    inferExpr(expr) {
        if (expr.filType)
            return expr.filType;
        let t = 'unknown';
        switch (expr.kind) {
            case 'Literal': {
                // Bigint literal already pre-tagged by parser
                if (expr.filType === 'i64') {
                    t = 'i64';
                    break;
                }
                if (typeof expr.value === 'boolean')
                    t = 'bool';
                else if (typeof expr.value === 'number') {
                    t = Number.isInteger(expr.value) ? 'i32' : 'f64';
                }
                else if (typeof expr.value === 'string')
                    t = 'string';
                else
                    t = 'json'; // null/undefined
                break;
            }
            case 'Identifier': {
                const varType = this.lookupVar(expr.name);
                if (varType)
                    t = varType;
                else if (this.actionNames.has(expr.name) || this.triggerNames.has(expr.name)) {
                    // Action/trigger identifiers are only legal in a context that
                    // handles them explicitly (currently: executeNode's first arg,
                    // which short-circuits before this case). A bare reference is a
                    // misuse — fail with an actionable message.
                    throw new Error(`action/trigger identifier "${expr.name}" can only be used as the first argument to executeNode`);
                }
                else {
                    // Could be a global/built-in
                    t = 'unknown';
                }
                break;
            }
            case 'TemplateLiteral':
                t = 'string';
                for (const e of expr.expressions)
                    this.inferExpr(e);
                break;
            case 'ArrayExpression': {
                let elemType = 'json';
                if (expr.elements.length > 0) {
                    elemType = this.inferExpr(expr.elements[0]);
                }
                for (const e of expr.elements)
                    this.inferExpr(e);
                t = { kind: 'array', element: elemType };
                break;
            }
            case 'ObjectExpression': {
                for (const p of expr.properties)
                    this.inferExpr(p.value);
                t = 'json';
                break;
            }
            case 'SpreadElement':
                this.inferExpr(expr.argument);
                t = 'json';
                break;
            case 'AssignmentExpression': {
                const rhsType = this.inferExpr(expr.right);
                this.inferExpr(expr.left);
                t = rhsType;
                break;
            }
            case 'BinaryExpression': {
                const lt = this.inferExpr(expr.left);
                const rt = this.inferExpr(expr.right);
                const op = expr.operator;
                // Promote integer literals when the other operand is a wider numeric
                // type. The parser tags `1` as i32 and `1n` as i64, but in a context
                // like `daysAgo > 7` (where `daysAgo: f64`) the literal `7` should
                // be treated as `7.0` so the WAT emitter picks `f64.const`. Without
                // this, the emitter produces an `f64.gt`-with-`i32.const` operand
                // mismatch that V8 rejects with "expected type f64, found i32.const".
                if (lt === 'f64' && rt === 'i32')
                    coerceNumericLiteral(expr.right, 'f64');
                else if (lt === 'i32' && rt === 'f64')
                    coerceNumericLiteral(expr.left, 'f64');
                else if (lt === 'i64' && rt === 'i32')
                    coerceNumericLiteral(expr.right, 'i64');
                else if (lt === 'i32' && rt === 'i64')
                    coerceNumericLiteral(expr.left, 'i64');
                if (['==', '!=', '===', '!==', '<', '<=', '>', '>=', '&&', '||', 'instanceof', 'in'].includes(op)) {
                    t = 'bool';
                }
                else if (lt === 'i64' || rt === 'i64' || lt === 'DateTime' || rt === 'DateTime' || lt === 'TimeSpan' || rt === 'TimeSpan') {
                    // Preserve i64-shaped types for arithmetic so the WAT emitter knows to use i64 ops.
                    if (lt === 'DateTime' || rt === 'DateTime')
                        t = 'DateTime';
                    else if (lt === 'TimeSpan' || rt === 'TimeSpan')
                        t = 'TimeSpan';
                    else
                        t = 'i64';
                }
                else if (op === '+') {
                    // If either operand is string, result is string
                    if (lt === 'string' || rt === 'string')
                        t = 'string';
                    else if (lt === 'f64' || rt === 'f64')
                        t = 'f64';
                    else
                        t = 'i32';
                }
                else if (['-', '*', '/', '%'].includes(op)) {
                    if (lt === 'f64' || rt === 'f64')
                        t = 'f64';
                    else
                        t = 'i32';
                }
                else if (['**'].includes(op)) {
                    t = 'f64';
                }
                else {
                    t = 'i32'; // bitwise ops
                }
                break;
            }
            case 'LogicalExpression': {
                const lt = this.inferExpr(expr.left);
                this.inferExpr(expr.right);
                t = lt; // logical ops return one of the operands
                break;
            }
            case 'UnaryExpression': {
                const argType = this.inferExpr(expr.argument);
                if (expr.operator === '!')
                    t = 'bool';
                else if (expr.operator === 'typeof')
                    t = 'string';
                else
                    t = argType;
                break;
            }
            case 'UpdateExpression': {
                t = this.inferExpr(expr.argument);
                break;
            }
            case 'ConditionalExpression': {
                this.inferExpr(expr.test);
                const ct = this.inferExpr(expr.consequent);
                this.inferExpr(expr.alternate);
                t = ct;
                break;
            }
            case 'CallExpression': {
                t = this.inferCallType(expr);
                break;
            }
            case 'MemberExpression': {
                const objType = this.inferExpr(expr.object);
                if (expr.property.kind === 'Identifier') {
                    const propName = expr.property.name;
                    if (propName === 'length')
                        t = 'i32';
                    else if (['includes', 'startsWith', 'endsWith', 'hasOwnProperty'].includes(propName))
                        t = 'bool';
                    else if (['indexOf', 'lastIndexOf', 'charCodeAt'].includes(propName))
                        t = 'i32';
                    else if (['charAt', 'slice', 'substring', 'toUpperCase', 'toLowerCase',
                        'trim', 'trimStart', 'trimEnd', 'replace', 'replaceAll',
                        'padStart', 'padEnd', 'repeat', 'concat'].includes(propName))
                        t = 'string';
                    else if (propName === 'split')
                        t = { kind: 'array', element: 'string' };
                    else if (propName === 'join')
                        t = 'string';
                    else
                        t = 'json';
                }
                else {
                    this.inferExpr(expr.property);
                    t = 'json';
                }
                break;
            }
            case 'AwaitExpression': {
                const innerType = this.inferExpr(expr.argument);
                if (AST.isPromiseType(innerType)) {
                    t = innerType.inner;
                }
                else {
                    t = innerType;
                }
                break;
            }
            case 'NewExpression': {
                // new Error(...) returns json (error object)
                t = 'json';
                for (const a of expr.arguments)
                    this.inferExpr(a);
                break;
            }
            case 'TypeAssertion': {
                this.inferExpr(expr.expression);
                t = expr.assertedType;
                break;
            }
            case 'SequenceExpression': {
                for (const e of expr.expressions)
                    t = this.inferExpr(e);
                break;
            }
        }
        expr.filType = t;
        return t;
    }
    inferCallType(expr) {
        const callee = expr.callee;
        // Direct function call
        if (callee.kind === 'Identifier') {
            const name = callee.name;
            if (name === 'executeNode') {
                // First arg MUST be an identifier resolving to a declared `action`.
                // Trigger identifiers and string literals are rejected — strings are
                // the legacy form we're dropping; triggers fire events into the
                // flow, they aren't dispatched.
                if (expr.arguments.length < 1) {
                    throw new Error('executeNode requires at least one argument (action identifier)');
                }
                const first = expr.arguments[0];
                if (first.kind !== 'Identifier') {
                    throw new Error(`executeNode's first argument must be an action identifier ` +
                        `(got ${first.kind}). String-literal node names are no longer supported.`);
                }
                if (this.triggerNames.has(first.name)) {
                    throw new Error(`executeNode cannot dispatch trigger "${first.name}" — triggers fire events into the flow, ` +
                        `they aren't invoked. Declare and use an \`action\` instead.`);
                }
                if (!this.actionNames.has(first.name)) {
                    throw new Error(`executeNode: "${first.name}" is not a declared action. ` +
                        `Add an \`action ${first.name}: <type> { … };\` declaration at the top level.`);
                }
                this.actionInvocationCounts.set(first.name, (this.actionInvocationCounts.get(first.name) ?? 0) + 1);
                // Type-check remaining args normally.
                for (let i = 1; i < expr.arguments.length; i++)
                    this.inferExpr(expr.arguments[i]);
                return { kind: 'Promise', inner: 'string' };
            }
            if (name === 'executeTimer') {
                for (const a of expr.arguments)
                    this.inferExpr(a);
                return { kind: 'Promise', inner: 'DateTime' };
            }
            if (name === 'getDateTime') {
                return 'DateTime';
            }
            if (name === 'getUuid') {
                return 'string';
            }
            if (name === 'console')
                return 'void';
            // User-defined function
            const fnInfo = this.functions.get(name);
            if (fnInfo) {
                for (const a of expr.arguments)
                    this.inferExpr(a);
                if (fnInfo.isAsync) {
                    return { kind: 'Promise', inner: AST.unwrapPromise(fnInfo.returnType) };
                }
                return fnInfo.returnType;
            }
            return 'json';
        }
        // Method call: obj.method(...)
        if (callee.kind === 'MemberExpression') {
            return this.inferMethodCallType(callee, expr.arguments);
        }
        for (const a of expr.arguments)
            this.inferExpr(a);
        return 'json';
    }
    inferMethodCallType(member, args) {
        for (const a of args)
            this.inferExpr(a);
        const obj = member.object;
        const propExpr = member.property;
        if (propExpr.kind !== 'Identifier')
            return 'json';
        const prop = propExpr.name;
        // Check for namespace calls: Math.xxx, JSON.xxx, Number.xxx, Object.xxx, Array.xxx, String.xxx, console.xxx
        if (obj.kind === 'Identifier') {
            const ns = obj.name;
            if (ns === 'Math') {
                if (prop === 'PI' || prop === 'E')
                    return 'f64';
                if (['abs', 'ceil', 'floor', 'round', 'sqrt', 'pow', 'min', 'max',
                    'log', 'log2', 'log10', 'sin', 'cos', 'tan', 'random', 'trunc', 'sign'].includes(prop)) {
                    return 'f64';
                }
                return 'f64';
            }
            if (ns === 'JSON') {
                if (prop === 'stringify')
                    return 'string';
                if (prop === 'parse')
                    return 'json';
                return 'json';
            }
            if (ns === 'Number') {
                if (prop === 'parseInt')
                    return 'i32';
                if (prop === 'parseFloat')
                    return 'f64';
                if (['isInteger', 'isFinite', 'isNaN'].includes(prop))
                    return 'bool';
                if (prop === 'MAX_SAFE_INTEGER' || prop === 'MIN_SAFE_INTEGER')
                    return 'i32';
                return 'json';
            }
            if (ns === 'Object') {
                if (['keys', 'values'].includes(prop))
                    return { kind: 'array', element: 'string' };
                if (prop === 'entries')
                    return { kind: 'array', element: 'json' };
                if (prop === 'hasOwn')
                    return 'bool';
                return 'json';
            }
            if (ns === 'Array') {
                if (prop === 'isArray')
                    return 'bool';
                return 'json';
            }
            if (ns === 'String') {
                if (prop === 'fromCharCode')
                    return 'string';
                return 'string';
            }
            if (ns === 'console') {
                return 'void';
            }
            if (ns === 'DateTime') {
                if (prop === 'now' || prop === 'fromEpochMillis' || prop === 'fromISOString') {
                    return 'DateTime';
                }
                return 'DateTime';
            }
            if (ns === 'TimeSpan') {
                if (prop === 'fromMillis' || prop === 'fromSeconds' || prop === 'fromMinutes' ||
                    prop === 'fromHours' || prop === 'fromDays') {
                    return 'TimeSpan';
                }
                return 'TimeSpan';
            }
            if (ns === 'Promise') {
                // Promise.all returns a json container (indexable with [i]).
                // Promise.any returns just the winner index (i32). The winner's value
                // is not exposed directly — callers are expected to structure their
                // post-race code so they don't need it (e.g. SLA escalation paths).
                if (prop === 'all')
                    return { kind: 'Promise', inner: 'json' };
                if (prop === 'any')
                    return { kind: 'Promise', inner: 'i32' };
                return 'json';
            }
        }
        // Infer object type first
        const objType = this.inferExpr(obj);
        // String methods
        if (objType === 'string') {
            if (prop === 'length')
                return 'i32';
            if (prop === 'charAt')
                return 'string';
            if (prop === 'charCodeAt' || prop === 'indexOf' || prop === 'lastIndexOf')
                return 'i32';
            if (['includes', 'startsWith', 'endsWith'].includes(prop))
                return 'bool';
            if (['slice', 'substring', 'toUpperCase', 'toLowerCase', 'trim', 'trimStart',
                'trimEnd', 'replace', 'replaceAll', 'padStart', 'padEnd', 'repeat', 'concat'].includes(prop))
                return 'string';
            if (prop === 'split')
                return { kind: 'array', element: 'string' };
            if (prop === 'toString' || prop === 'toFixed')
                return 'string';
            return 'json';
        }
        // Array methods
        if (AST.isArrayType(objType)) {
            if (prop === 'length')
                return 'i32';
            if (prop === 'push' || prop === 'unshift')
                return 'i32';
            if (prop === 'pop' || prop === 'shift')
                return objType.element;
            if (prop === 'indexOf')
                return 'i32';
            if (prop === 'includes')
                return 'bool';
            if (prop === 'join')
                return 'string';
            if (prop === 'slice' || prop === 'concat' || prop === 'reverse' || prop === 'sort' ||
                prop === 'flat' || prop === 'fill')
                return objType;
            if (prop === 'splice')
                return objType;
            if (prop === 'map')
                return { kind: 'array', element: 'json' };
            if (prop === 'filter')
                return objType;
            if (prop === 'reduce')
                return 'json';
            if (['some', 'every'].includes(prop))
                return 'bool';
            if (prop === 'find' || prop === 'findIndex')
                return objType.element;
            if (prop === 'forEach')
                return 'void';
            return 'json';
        }
        // Number methods
        if (objType === 'i32' || objType === 'f64') {
            if (prop === 'toString' || prop === 'toFixed')
                return 'string';
            return 'json';
        }
        // DateTime instance methods
        if (objType === 'DateTime') {
            if (prop === 'add' || prop === 'subtract')
                return 'DateTime';
            if (prop === 'diff')
                return 'TimeSpan';
            if (prop === 'toEpochMillis')
                return 'i64';
            if (prop === 'toISOString')
                return 'string';
            if (prop === 'getYear' || prop === 'getMonth' || prop === 'getDay' ||
                prop === 'getDayOfWeek' || prop === 'getHour' || prop === 'getMinute' ||
                prop === 'getSecond' || prop === 'getMillisecond')
                return 'i32';
            if (prop === 'equals' || prop === 'isBefore' || prop === 'isAfter')
                return 'bool';
            return 'json';
        }
        // TimeSpan instance methods
        if (objType === 'TimeSpan') {
            if (prop === 'add' || prop === 'subtract' || prop === 'multiply' || prop === 'negate')
                return 'TimeSpan';
            if (prop === 'totalMillis')
                return 'i64';
            if (prop === 'totalSeconds' || prop === 'totalMinutes' || prop === 'totalHours' ||
                prop === 'totalDays')
                return 'f64';
            if (prop === 'equals')
                return 'bool';
            return 'json';
        }
        return 'json';
    }
    getFunctions() {
        return this.functions;
    }
}
exports.TypeChecker = TypeChecker;
//# sourceMappingURL=typechecker.js.map