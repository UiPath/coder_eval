"use strict";
/**
 * FIL DateTime/TimeSpan → v1 JS Date translation.
 *
 * FIL has first-class `DateTime` and `TimeSpan` types with a known method
 * surface (see fil/src/typechecker.ts §"DateTime instance methods"). The
 * v1 .flow evaluator (Jint) is plain JS and has no equivalent — both
 * types lower to JS `number` (ms timestamp / ms duration), with method
 * calls rewritten to `Date`/arithmetic expressions.
 *
 * Used by `script-body-emitter` and `convertFILExprToFlowExpr` to
 * rewrite known DateTime/TimeSpan call sites at emit time. Returns
 * `null` when the call doesn't match a known DateTime/TimeSpan pattern,
 * letting the caller fall through to its normal handling.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.tryTranslateDateTimeCall = tryTranslateDateTimeCall;
/**
 * If `expr` is a CallExpression matching a known DateTime/TimeSpan
 * static or instance method, returns the v1 JS equivalent as a string.
 * Returns `null` otherwise.
 */
function tryTranslateDateTimeCall(expr, emit) {
    const callee = expr.callee;
    if (callee.kind !== 'MemberExpression' || callee.computed)
        return null;
    if (callee.property.kind !== 'Identifier')
        return null;
    const methodName = callee.property.name;
    const obj = callee.object;
    // ── Static factories: DateTime.* / TimeSpan.* ────────────────────────
    if (obj.kind === 'Identifier') {
        const ns = obj.name;
        if (ns === 'DateTime') {
            return translateDateTimeStatic(methodName, expr.arguments, emit);
        }
        if (ns === 'TimeSpan') {
            return translateTimeSpanStatic(methodName, expr.arguments, emit);
        }
    }
    // ── Instance methods: `<dt>.method(<args>)` / `<ts>.method(<args>)` ─
    // We can't distinguish DateTime vs TimeSpan vs unrelated calls at
    // emit time without a type table. Use method-name heuristics — the
    // DateTime/TimeSpan API is distinctive enough that collisions with
    // user code are unlikely (e.g. `.totalDays` / `.toEpochMillis` /
    // `.isBefore` are TimeSpan/DateTime-exclusive names).
    return translateDateTimeInstance(methodName, obj, expr.arguments, emit);
}
function translateDateTimeStatic(method, args, emit) {
    switch (method) {
        case 'now':
            return 'Date.now()';
        case 'fromEpochMillis':
            // Already a ms timestamp; pass through (Number() for safety).
            return args.length ? `Number(${emit(args[0])})` : null;
        case 'fromISOString':
            return args.length ? `new Date(${emit(args[0])}).getTime()` : null;
    }
    return null;
}
function translateTimeSpanStatic(method, args, emit) {
    if (!args.length)
        return null;
    const a = emit(args[0]);
    switch (method) {
        case 'fromMillis': return `Number(${a})`;
        case 'fromSeconds': return `(${a} * 1000)`;
        case 'fromMinutes': return `(${a} * 60000)`;
        case 'fromHours': return `(${a} * 3600000)`;
        case 'fromDays': return `(${a} * 86400000)`;
    }
    return null;
}
function translateDateTimeInstance(method, obj, args, emit) {
    const o = emit(obj);
    switch (method) {
        // DateTime instance
        case 'add':
            return args.length ? `(${o} + ${emit(args[0])})` : null;
        case 'subtract':
            return args.length ? `(${o} - ${emit(args[0])})` : null;
        case 'diff':
            return args.length ? `(${o} - ${emit(args[0])})` : null;
        case 'toEpochMillis':
            return o;
        case 'toISOString':
            return `new Date(${o}).toISOString()`;
        case 'getYear': return `new Date(${o}).getFullYear()`;
        case 'getMonth': return `(new Date(${o}).getMonth() + 1)`; // FIL is 1-based
        case 'getDay': return `new Date(${o}).getDate()`;
        case 'getDayOfWeek': return `new Date(${o}).getDay()`;
        case 'getHour': return `new Date(${o}).getHours()`;
        case 'getMinute': return `new Date(${o}).getMinutes()`;
        case 'getSecond': return `new Date(${o}).getSeconds()`;
        case 'getMillisecond': return `new Date(${o}).getMilliseconds()`;
        case 'equals':
            return args.length ? `(${o} === ${emit(args[0])})` : null;
        case 'isBefore':
            return args.length ? `(${o} < ${emit(args[0])})` : null;
        case 'isAfter':
            return args.length ? `(${o} > ${emit(args[0])})` : null;
        // TimeSpan instance
        case 'totalMillis': return o;
        case 'totalSeconds': return `(${o} / 1000)`;
        case 'totalMinutes': return `(${o} / 60000)`;
        case 'totalHours': return `(${o} / 3600000)`;
        case 'totalDays': return `(${o} / 86400000)`;
        case 'multiply':
            return args.length ? `(${o} * ${emit(args[0])})` : null;
        case 'negate':
            return `(-${o})`;
    }
    return null;
}
//# sourceMappingURL=datetime-translator.js.map