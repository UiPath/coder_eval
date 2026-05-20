"use strict";
/**
 * Scope-aware FIL-to-JS expression emitter for v1 `core.action.script`
 * bodies. Mirrors `v1-to-v2`'s `emitExpression` but, for `Identifier`,
 * checks the converter's `Scope` and emits the resolved v1 path
 * (`$vars.<nodeId>.<outputId>`, `$vars.<inputName>`, etc.) instead of
 * the bare FIL name.
 *
 * Background: an `async function main()` body that does
 *
 *   const rawBatch: string = await executeNode(fetchBatch, "{}");
 *   const batch: json = JSON.parse(rawBatch);
 *
 * compiles to a v1 script node whose body is the JS for the second
 * statement. The naive FIL-emitter form would be
 *
 *   return JSON.parse(rawBatch);
 *
 * but `rawBatch` is not a JS local inside that script body — the v1
 * runtime evaluator has no way to resolve it. The fix is to substitute
 * each identifier through the converter's scope, producing
 *
 *   return JSON.parse($vars.fetchBatch.output);
 *
 * which v1 evaluates correctly.
 *
 * Identifiers that aren't in scope (the JS standard library — `JSON`,
 * `Math`, `Number`, etc. — plus locals introduced inside the script
 * body itself, e.g. by `coalesceConstScriptChains`) are left verbatim.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.emitScriptBodyExpression = emitScriptBodyExpression;
const datetime_translator_1 = require("./datetime-translator");
function emitScriptBodyExpression(expr, scope) {
    return emit(expr, scope);
}
/**
 * True when `path` is something other than the FIL identifier itself —
 * meaning the converter has determined the v1 expression for this name.
 *
 * In v1 .flow JS script bodies, every identifier that refers to a flow
 * variable (input, output, mutated local, node output, loop iterator)
 * must be addressed via `$vars.<…>`. The v1 evaluator does NOT inject
 * flow variables as bare globals into the script's JS scope. So this
 * predicate is now permissive: any scope mapping means "substitute"
 * unless the path happens to equal the identifier itself.
 *
 * Script-local declarations (added by coalesceConstScriptChains as
 * `const <local> = …` on earlier lines of the bundled body) are NOT
 * in scope here — coalesce post-processes the bundled body to rewrite
 * `$vars.<local>` back to bare `<local>` once it knows which names
 * became JS locals.
 */
function needsSubstitution(path) {
    return path !== undefined;
}
function emit(expr, scope) {
    switch (expr.kind) {
        case 'Identifier': {
            const path = scope.get(expr.name);
            return needsSubstitution(path) ? path : expr.name;
        }
        case 'Literal':
            return expr.raw;
        case 'TemplateLiteral': {
            let result = '`';
            for (let i = 0; i < expr.quasis.length; i++) {
                result += expr.quasis[i];
                if (i < expr.expressions.length) {
                    result += '${' + emit(expr.expressions[i], scope) + '}';
                }
            }
            result += '`';
            return result;
        }
        case 'ArrayExpression':
            return `[${expr.elements.map(e => emit(e, scope)).join(', ')}]`;
        case 'ObjectExpression': {
            if (expr.properties.length === 0)
                return '{}';
            const props = expr.properties.map(p => {
                const key = p.key;
                if (p.shorthand) {
                    // Shorthand `{ foo }` is equivalent to `{ foo: foo }`. Expand
                    // to `{ foo: <resolved> }` only when the resolved path is a
                    // node-output reference (multi-segment $vars path).
                    const resolved = scope.get(key);
                    if (needsSubstitution(resolved)) {
                        return `${key}: ${resolved}`;
                    }
                    return key;
                }
                return `${emitPropertyKey(key)}: ${emit(p.value, scope)}`;
            });
            return `{ ${props.join(', ')} }`;
        }
        case 'SpreadElement':
            return `...${emit(expr.argument, scope)}`;
        case 'AssignmentExpression':
            return `${emit(expr.left, scope)} ${expr.operator} ${emit(expr.right, scope)}`;
        case 'BinaryExpression':
            return `(${emit(expr.left, scope)} ${expr.operator} ${emit(expr.right, scope)})`;
        case 'LogicalExpression':
            return `(${emit(expr.left, scope)} ${expr.operator} ${emit(expr.right, scope)})`;
        case 'UnaryExpression':
            if (expr.prefix) {
                const space = expr.operator.length > 1 ? ' ' : '';
                return `${expr.operator}${space}${emit(expr.argument, scope)}`;
            }
            return `${emit(expr.argument, scope)}${expr.operator}`;
        case 'UpdateExpression':
            if (expr.prefix)
                return `${expr.operator}${emit(expr.argument, scope)}`;
            return `${emit(expr.argument, scope)}${expr.operator}`;
        case 'ConditionalExpression':
            return `(${emit(expr.test, scope)} ? ${emit(expr.consequent, scope)} : ${emit(expr.alternate, scope)})`;
        case 'CallExpression': {
            // FIL's first-class DateTime / TimeSpan types don't exist in v1's
            // JS evaluator — rewrite known method calls to `Date.now()` /
            // `new Date(s).getTime()` / arithmetic before falling through to
            // the generic call emission.
            const dt = (0, datetime_translator_1.tryTranslateDateTimeCall)(expr, (e) => emit(e, scope));
            if (dt !== null)
                return dt;
            // For calls, the callee may be `JSON.stringify` or `obj.method(...)`.
            // We want to pass the callee through emit so that obj resolves via
            // scope (e.g. `myArr.map(...)` where myArr is in scope) but the
            // method name on a MemberExpression must stay verbatim — handled
            // by the MemberExpression case below.
            const callee = emit(expr.callee, scope);
            const args = expr.arguments.map(a => emit(a, scope)).join(', ');
            return `${callee}(${args})`;
        }
        case 'MemberExpression': {
            const obj = emit(expr.object, scope);
            if (expr.computed) {
                const opt = expr.optional ? '?.' : '';
                return `${obj}${opt}[${emit(expr.property, scope)}]`;
            }
            // Non-computed: property name is verbatim (it's a static key, not
            // a scope-resolvable identifier).
            const dot = expr.optional ? '?.' : '.';
            const propName = expr.property.kind === 'Identifier' ? expr.property.name : emit(expr.property, scope);
            return `${obj}${dot}${propName}`;
        }
        case 'AwaitExpression':
            // Shouldn't appear inside a sync script body — but pass through
            // defensively to avoid a silent drop.
            return `await ${emit(expr.argument, scope)}`;
        case 'NewExpression': {
            const callee = emit(expr.callee, scope);
            const args = expr.arguments.map(a => emit(a, scope)).join(', ');
            return `new ${callee}(${args})`;
        }
        case 'TypeAssertion':
            // TS-only; v1 JS just takes the underlying expression.
            return emit(expr.expression, scope);
        case 'SequenceExpression':
            return expr.expressions.map(e => emit(e, scope)).join(', ');
        default:
            return `/* unknown expr: ${expr.kind} */`;
    }
}
function emitPropertyKey(key) {
    // Quote non-identifier keys; otherwise emit bare.
    if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key))
        return key;
    return JSON.stringify(key);
}
//# sourceMappingURL=script-body-emitter.js.map