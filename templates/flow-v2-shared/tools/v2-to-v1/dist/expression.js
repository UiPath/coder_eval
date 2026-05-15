"use strict";
/**
 * Phase A — classify FIL expressions and emit v1 `=js:` expression strings
 * when possible.
 *
 * The v1 BPMN engine evaluates expressions via two backends; the JS one is
 * `=js:<source>`, fed verbatim to a Jint engine. Jint supports the full
 * JavaScript language, so the constraint isn't syntax: it's
 *
 *   1. **No side effects.** The Jint engine evaluates one expression and
 *      returns a value. Assignments are blocked elsewhere in the pipeline,
 *      and any mutation we generate can't propagate to the surrounding flow.
 *      Therefore: no `=`, `++`, `--`, `await`, `new`.
 *   2. **Identifiers must resolve to context-bound names.** The engine
 *      registers flow variables under nested namespaces — `vars.foo`,
 *      `result.bar`, `outputs.x`, `iterator.i`, `metadata.*`, etc. (see
 *      BpmnExpressionNamespace.cs). A bare FIL identifier like `userId`
 *      maps to a v1 expression iff `userId` is a known flow variable.
 *
 * This module exports `tryEmitV1Expression(expr, scope)` which returns:
 *   - a `=js:`-prefixed string when the expression is fully expressible,
 *   - `null` when any sub-expression falls outside the supported subset
 *     (caller falls back to script-node emission or fails the conversion).
 *
 * Supported AST shapes (initial set — expand as needs arise):
 *   Literal, TemplateLiteral (no interpolations or all-resolvable),
 *   Identifier (when in scope), MemberExpression, ArrayExpression,
 *   ObjectExpression, BinaryExpression, LogicalExpression, UnaryExpression
 *   (`-` and `!`), ConditionalExpression, TypeAssertion (transparent),
 *   CallExpression (whitelisted: JSON.stringify, JSON.parse, Math.*,
 *   String.fromCharCode, Number.parse*, plus `.length` etc. as member access).
 *
 * NOT supported (returns null):
 *   AwaitExpression, AssignmentExpression, UpdateExpression, NewExpression,
 *   SpreadElement, SequenceExpression, arbitrary CallExpression callees,
 *   identifier references outside `scope`.
 *
 * The emitter preserves operator precedence by parenthesising aggressively —
 * Jint reparses the source so excess parens are harmless.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.tryEmitV1Expression = tryEmitV1Expression;
/** Whitelisted member-call patterns: object name → method name → safe. */
const WHITELISTED_NAMESPACE_CALLS = {
    JSON: new Set(['stringify', 'parse']),
    Math: new Set([
        'abs', 'ceil', 'floor', 'round', 'sqrt', 'pow', 'min', 'max',
        'log', 'log2', 'log10', 'sin', 'cos', 'tan', 'trunc', 'sign',
    ]),
    String: new Set(['fromCharCode']),
    Number: new Set(['parseInt', 'parseFloat', 'isInteger', 'isFinite', 'isNaN']),
    Object: new Set(['keys', 'values', 'entries']),
    Array: new Set(['isArray']),
};
/** Whitelisted method calls on string/array instances. The receiver itself
 *  must independently emit; this set just gates the callee `.method` part. */
const WHITELISTED_INSTANCE_METHODS = new Set([
    // string
    'charAt', 'charCodeAt', 'indexOf', 'lastIndexOf', 'includes',
    'startsWith', 'endsWith', 'slice', 'substring', 'toUpperCase',
    'toLowerCase', 'trim', 'trimStart', 'trimEnd', 'split', 'replace',
    'replaceAll', 'padStart', 'padEnd', 'repeat', 'concat', 'toString',
    // array
    'join', 'slice', 'indexOf', 'includes', 'concat', 'reverse',
    // shared
    'length', 'flat',
]);
/** Whitelisted member properties (no call). */
const WHITELISTED_PROPERTIES = new Set(['length', 'PI', 'E', 'MAX_SAFE_INTEGER', 'MIN_SAFE_INTEGER']);
/**
 * JS-side namespace identifiers that the Jint engine exposes by default
 * even without a scope binding. Member access on these (`Math.PI`,
 * `JSON.stringify(...)`) is always safe.
 */
const SAFE_GLOBAL_NAMESPACES = new Set([
    'Math', 'JSON', 'Object', 'Array', 'String', 'Number',
]);
const PREFIX = '=js:';
/**
 * Attempt to emit the expression as a v1 expression. Returns null when any
 * sub-expression falls outside the supported subset.
 */
function tryEmitV1Expression(expr, scope, opts = {}) {
    const referenced = new Set();
    try {
        const body = emit(expr, scope, referenced);
        if (body === null)
            return null;
        return {
            expression: opts.bare ? body : PREFIX + body,
            referenced,
        };
    }
    catch {
        return null;
    }
}
function emit(expr, scope, referenced) {
    switch (expr.kind) {
        case 'Literal':
            return emitLiteral(expr);
        case 'TemplateLiteral':
            return emitTemplate(expr, scope, referenced);
        case 'Identifier':
            return emitIdentifier(expr, scope, referenced);
        case 'MemberExpression':
            return emitMember(expr, scope, referenced);
        case 'ArrayExpression':
            return emitArray(expr, scope, referenced);
        case 'ObjectExpression':
            return emitObject(expr, scope, referenced);
        case 'BinaryExpression':
            return emitBinary(expr, scope, referenced);
        case 'LogicalExpression':
            return emitLogical(expr, scope, referenced);
        case 'UnaryExpression':
            return emitUnary(expr, scope, referenced);
        case 'ConditionalExpression':
            return emitConditional(expr, scope, referenced);
        case 'CallExpression':
            return emitCall(expr, scope, referenced);
        case 'TypeAssertion':
            // `expr as T` — the TS type assertion is a no-op at runtime.
            return emit(expr.expression, scope, referenced);
        default:
            return null;
    }
}
function emitLiteral(expr) {
    if (expr.value === null)
        return 'null';
    if (typeof expr.value === 'boolean')
        return expr.value ? 'true' : 'false';
    if (typeof expr.value === 'number') {
        // FIL bigint literals (`1n`) tag filType: 'i64'. The Jint engine has
        // BigInt — emit with the suffix.
        if (expr.filType === 'i64')
            return `${expr.value}n`;
        return String(expr.value);
    }
    if (typeof expr.value === 'string')
        return JSON.stringify(expr.value);
    return 'null';
}
function emitTemplate(expr, scope, referenced) {
    if (expr.expressions.length === 0 && expr.quasis.length === 1) {
        return JSON.stringify(expr.quasis[0]);
    }
    // `Hello ${name}` — concatenate via `+` so the engine doesn't need to
    // support tagged-template semantics. Quasis alternate with expressions.
    const parts = [];
    for (let i = 0; i < expr.quasis.length; i++) {
        if (expr.quasis[i] !== '')
            parts.push(JSON.stringify(expr.quasis[i]));
        if (i < expr.expressions.length) {
            const inner = emit(expr.expressions[i], scope, referenced);
            if (inner === null)
                return null;
            parts.push(`(${inner})`);
        }
    }
    return parts.length === 0 ? '""' : parts.join(' + ');
}
function emitIdentifier(expr, scope, referenced) {
    const v1Path = scope.get(expr.name);
    if (v1Path === undefined)
        return null;
    referenced.add(expr.name);
    return v1Path;
}
function emitMember(expr, scope, referenced) {
    // Root-level namespace access: scope-bound identifier (`vars.foo`,
    // `metadata.X`) or a JS global (`Math.PI`, `JSON.stringify`).
    if (expr.object.kind === 'Identifier') {
        let objPath = null;
        if (scope.has(expr.object.name)) {
            referenced.add(expr.object.name);
            objPath = scope.get(expr.object.name);
        }
        else if (SAFE_GLOBAL_NAMESPACES.has(expr.object.name)) {
            objPath = expr.object.name;
        }
        if (objPath !== null) {
            if (expr.computed) {
                const propSrc = emit(expr.property, scope, referenced);
                if (propSrc === null)
                    return null;
                return `${objPath}[${propSrc}]`;
            }
            if (expr.property.kind !== 'Identifier')
                return null;
            return `${objPath}.${expr.property.name}`;
        }
        // Bare identifier not in scope and not a known global → can't resolve.
        return null;
    }
    // Recursive member access (e.g. `vars.user.profile.name`). Avoid wrapping
    // a chain that's already a clean dotted path — emitMember on the inner
    // node returns one already if it resolves cleanly.
    const objSrc = emit(expr.object, scope, referenced);
    if (objSrc === null)
        return null;
    const inner = isCleanMemberPath(expr.object) ? objSrc : `(${objSrc})`;
    if (expr.computed) {
        const propSrc = emit(expr.property, scope, referenced);
        if (propSrc === null)
            return null;
        return `${inner}[${propSrc}]`;
    }
    if (expr.property.kind !== 'Identifier')
        return null;
    return `${inner}.${expr.property.name}`;
}
/** True if the expression is itself a member-access path or identifier
 *  (no surrounding parens needed when used as a chain object). */
function isCleanMemberPath(expr) {
    return expr.kind === 'Identifier' || expr.kind === 'MemberExpression';
}
function emitArray(expr, scope, referenced) {
    const parts = [];
    for (const el of expr.elements) {
        if (el.kind === 'SpreadElement')
            return null;
        const src = emit(el, scope, referenced);
        if (src === null)
            return null;
        parts.push(src);
    }
    return `[${parts.join(', ')}]`;
}
function emitObject(expr, scope, referenced) {
    const parts = [];
    for (const prop of expr.properties) {
        const valueSrc = emit(prop.value, scope, referenced);
        if (valueSrc === null)
            return null;
        parts.push(`${JSON.stringify(prop.key)}: ${valueSrc}`);
    }
    return `({${parts.join(', ')}})`;
}
function emitBinary(expr, scope, referenced) {
    if (!isSafeBinaryOp(expr.operator))
        return null;
    const left = emit(expr.left, scope, referenced);
    const right = emit(expr.right, scope, referenced);
    if (left === null || right === null)
        return null;
    // FIL's `instanceof` and `in` operators map to JS but rarely make sense in
    // v1 expressions; allow them but parenthesise.
    return `(${left} ${expr.operator} ${right})`;
}
function isSafeBinaryOp(op) {
    return [
        '+', '-', '*', '/', '%', '**',
        '==', '!=', '===', '!==',
        '<', '<=', '>', '>=',
        '&', '|', '^', '<<', '>>', '>>>',
        'instanceof', 'in',
    ].includes(op);
}
function emitLogical(expr, scope, referenced) {
    const left = emit(expr.left, scope, referenced);
    const right = emit(expr.right, scope, referenced);
    if (left === null || right === null)
        return null;
    return `(${left} ${expr.operator} ${right})`;
}
function emitUnary(expr, scope, referenced) {
    if (!['-', '!', '+', '~', 'typeof'].includes(expr.operator))
        return null;
    const arg = emit(expr.argument, scope, referenced);
    if (arg === null)
        return null;
    return `${expr.operator}(${arg})`;
}
function emitConditional(expr, scope, referenced) {
    const test = emit(expr.test, scope, referenced);
    const cons = emit(expr.consequent, scope, referenced);
    const alt = emit(expr.alternate, scope, referenced);
    if (test === null || cons === null || alt === null)
        return null;
    return `(${test} ? ${cons} : ${alt})`;
}
function emitCall(expr, scope, referenced) {
    // Whitelisted namespace calls: JSON.stringify, Math.abs, etc.
    if (expr.callee.kind === 'MemberExpression' && !expr.callee.computed) {
        const obj = expr.callee.object;
        const prop = expr.callee.property;
        if (prop.kind !== 'Identifier')
            return null;
        // Namespace.method(...)
        if (obj.kind === 'Identifier' && WHITELISTED_NAMESPACE_CALLS[obj.name]?.has(prop.name)) {
            const args = emitArgs(expr.arguments, scope, referenced);
            if (args === null)
                return null;
            return `${obj.name}.${prop.name}(${args.join(', ')})`;
        }
        // <expr>.<instanceMethod>(...) — receiver must independently emit, and
        // the method must be on the safe list.
        if (WHITELISTED_INSTANCE_METHODS.has(prop.name)) {
            const recv = emit(obj, scope, referenced);
            if (recv === null)
                return null;
            const args = emitArgs(expr.arguments, scope, referenced);
            if (args === null)
                return null;
            return `(${recv}).${prop.name}(${args.join(', ')})`;
        }
    }
    // Bare-identifier calls (`foo(...)`) are rejected — there's no safe global
    // function in the BPMN context to call without `Math.`/`JSON.` etc.
    return null;
}
function emitArgs(args, scope, referenced) {
    const out = [];
    for (const a of args) {
        if (a.kind === 'SpreadElement')
            return null;
        const src = emit(a, scope, referenced);
        if (src === null)
            return null;
        out.push(src);
    }
    return out;
}
//# sourceMappingURL=expression.js.map