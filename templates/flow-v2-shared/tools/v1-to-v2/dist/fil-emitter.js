"use strict";
/**
 * Pretty-prints a FIL AST back to valid FIL/TypeScript source code.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.emitProgram = emitProgram;
exports.emitType = emitType;
exports.emitExpression = emitExpression;
function emitProgram(program) {
    const parts = [];
    if (program.flow)
        parts.push(emitFlowDeclaration(program.flow));
    for (const t of program.triggers)
        parts.push(emitTriggerDeclaration(t));
    for (const a of program.actions)
        parts.push(emitActionDeclaration(a));
    for (const fn of program.functions)
        parts.push(emitFunction(fn));
    return parts.join('\n\n') + '\n';
}
function emitFlowDeclaration(flow) {
    return `flow ${flow.id} ${emitObjectLiteral(flow.fields)};`;
}
function emitActionDeclaration(action) {
    const head = `action ${action.name}: ${emitTypeRef(action.typeRef)}`;
    if (action.fields && action.fields.properties.length > 0) {
        return `${head} ${emitObjectLiteral(action.fields)};`;
    }
    return `${head};`;
}
function emitTriggerDeclaration(trigger) {
    const head = `trigger ${trigger.name}: ${emitTypeRef(trigger.typeRef)}`;
    if (trigger.fields && trigger.fields.properties.length > 0) {
        return `${head} ${emitObjectLiteral(trigger.fields)};`;
    }
    return `${head};`;
}
function emitTypeRef(ref) {
    return ref.version ? `${ref.name}@${ref.version}` : ref.name;
}
function emitObjectLiteral(obj) {
    if (obj.properties.length === 0)
        return '{}';
    const props = obj.properties.map((p) => `  ${emitPropertyKey(p.key)}: ${emitExpression(p.value)}`).join(',\n');
    return `{\n${props},\n}`;
}
const VALID_IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
function emitPropertyKey(key) {
    // Bare identifiers parse as object-literal keys; anything else (dotted
    // names, kebab-case, leading digits, …) needs to be quoted so the FIL
    // parser accepts it as a `StringLiteral` key.
    if (VALID_IDENT.test(key))
        return key;
    return JSON.stringify(key);
}
function emitFunction(fn) {
    const async = fn.isAsync ? 'async ' : '';
    const params = fn.params.map(p => `${p.name}: ${emitType(p.type)}`).join(', ');
    const ret = emitType(fn.returnType);
    const body = emitBlock(fn.body, 1);
    return `${async}function ${fn.name}(${params}): ${ret} {\n${body}}`;
}
function emitType(t) {
    if (typeof t === 'string')
        return t;
    if (t.kind === 'Promise')
        return `Promise<${emitType(t.inner)}>`;
    if (t.kind === 'array')
        return `${emitType(t.element)}[]`;
    return 'unknown';
}
function emitBlock(block, indent) {
    return block.body.map(s => emitStatement(s, indent)).join('');
}
function ind(level) {
    return '  '.repeat(level);
}
function emitStatement(stmt, indent) {
    switch (stmt.kind) {
        case 'BlockStatement':
            return `${ind(indent)}{\n${emitBlock(stmt, indent + 1)}${ind(indent)}}\n`;
        case 'VariableDeclaration': {
            const typeAnn = stmt.typeAnnotation ? `: ${emitType(stmt.typeAnnotation)}` : '';
            const init = stmt.initializer ? ` = ${emitExpr(stmt.initializer)}` : '';
            return `${ind(indent)}${stmt.declarationKind} ${stmt.name}${typeAnn}${init};\n`;
        }
        case 'ExpressionStatement':
            return `${ind(indent)}${emitExpr(stmt.expression)};\n`;
        case 'ReturnStatement':
            if (stmt.argument) {
                return `${ind(indent)}return ${emitExpr(stmt.argument)};\n`;
            }
            return `${ind(indent)}return;\n`;
        case 'ThrowStatement':
            return `${ind(indent)}throw ${emitExpr(stmt.argument)};\n`;
        case 'IfStatement': {
            let result = `${ind(indent)}if (${emitExpr(stmt.test)}) `;
            result += emitBodyOrBlock(stmt.consequent, indent);
            if (stmt.alternate) {
                // Trim trailing newline for else
                result = result.trimEnd() + ' else ';
                if (stmt.alternate.kind === 'IfStatement') {
                    // else if — don't add braces
                    result += emitStatement(stmt.alternate, indent).trimStart();
                }
                else {
                    result += emitBodyOrBlock(stmt.alternate, indent);
                }
            }
            return result;
        }
        case 'WhileStatement': {
            const label = stmt.label ? `${stmt.label}: ` : '';
            return `${label}${ind(indent)}while (${emitExpr(stmt.test)}) ${emitBodyOrBlock(stmt.body, indent)}`;
        }
        case 'ForStatement': {
            const label = stmt.label ? `${stmt.label}: ` : '';
            const init = stmt.init ? emitForInit(stmt.init) : '';
            const test = stmt.test ? emitExpr(stmt.test) : '';
            const update = stmt.update ? emitExpr(stmt.update) : '';
            return `${label}${ind(indent)}for (${init}; ${test}; ${update}) ${emitBodyOrBlock(stmt.body, indent)}`;
        }
        case 'ForOfStatement': {
            const label = stmt.label ? `${stmt.label}: ` : '';
            return `${label}${ind(indent)}for (${stmt.declarationKind} ${stmt.item} of ${emitExpr(stmt.iterable)}) ${emitBodyOrBlock(stmt.body, indent)}`;
        }
        case 'ForInStatement': {
            const label = stmt.label ? `${stmt.label}: ` : '';
            return `${label}${ind(indent)}for (${stmt.declarationKind} ${stmt.key} in ${emitExpr(stmt.object)}) ${emitBodyOrBlock(stmt.body, indent)}`;
        }
        case 'SwitchStatement': {
            let result = `${ind(indent)}switch (${emitExpr(stmt.discriminant)}) {\n`;
            for (const c of stmt.cases) {
                if (c.test) {
                    result += `${ind(indent + 1)}case ${emitExpr(c.test)}:\n`;
                }
                else {
                    result += `${ind(indent + 1)}default:\n`;
                }
                for (const s of c.consequent) {
                    result += emitStatement(s, indent + 2);
                }
            }
            result += `${ind(indent)}}\n`;
            return result;
        }
        case 'TryStatement': {
            let result = `${ind(indent)}try {\n${emitBlock(stmt.block, indent + 1)}${ind(indent)}}`;
            if (stmt.handler) {
                const param = stmt.handler.param ? ` (${stmt.handler.param})` : '';
                result += ` catch${param} {\n${emitBlock(stmt.handler.body, indent + 1)}${ind(indent)}}`;
            }
            if (stmt.finalizer) {
                result += ` finally {\n${emitBlock(stmt.finalizer, indent + 1)}${ind(indent)}}`;
            }
            result += '\n';
            return result;
        }
        case 'BreakStatement':
            return `${ind(indent)}break${stmt.label ? ' ' + stmt.label : ''};\n`;
        case 'ContinueStatement':
            return `${ind(indent)}continue${stmt.label ? ' ' + stmt.label : ''};\n`;
        case 'LabeledStatement':
            return `${ind(indent)}${stmt.label}: ${emitStatement(stmt.body, indent).trimStart()}`;
        default:
            return `${ind(indent)}/* unknown statement: ${stmt.kind} */\n`;
    }
}
function emitBodyOrBlock(stmt, indent) {
    if (stmt.kind === 'BlockStatement') {
        return `{\n${emitBlock(stmt, indent + 1)}${ind(indent)}}\n`;
    }
    // Wrap single statement in block
    return `{\n${emitStatement(stmt, indent + 1)}${ind(indent)}}\n`;
}
function emitForInit(init) {
    if (init.kind === 'VariableDeclaration') {
        const typeAnn = init.typeAnnotation ? `: ${emitType(init.typeAnnotation)}` : '';
        const initializer = init.initializer ? ` = ${emitExpr(init.initializer)}` : '';
        return `${init.declarationKind} ${init.name}${typeAnn}${initializer}`;
    }
    return emitExpr(init.expression);
}
/**
 * Emit a single FIL expression as TypeScript/JS source. Public for
 * callers that need to embed a FIL expression inside another shape (e.g.
 * a v1 `core.action.script` body's `return <expr>;`).
 */
function emitExpression(expr) {
    return emitExpr(expr);
}
function emitExpr(expr) {
    switch (expr.kind) {
        case 'Identifier':
            return expr.name;
        case 'Literal':
            return expr.raw;
        case 'TemplateLiteral': {
            let result = '`';
            for (let i = 0; i < expr.quasis.length; i++) {
                result += expr.quasis[i];
                if (i < expr.expressions.length) {
                    result += '${' + emitExpr(expr.expressions[i]) + '}';
                }
            }
            result += '`';
            return result;
        }
        case 'ArrayExpression':
            return `[${expr.elements.map(emitExpr).join(', ')}]`;
        case 'ObjectExpression': {
            if (expr.properties.length === 0)
                return '{}';
            const props = expr.properties.map(p => {
                if (p.shorthand)
                    return p.key;
                return `${emitPropertyKey(p.key)}: ${emitExpr(p.value)}`;
            });
            return `{ ${props.join(', ')} }`;
        }
        case 'SpreadElement':
            return `...${emitExpr(expr.argument)}`;
        case 'AssignmentExpression':
            return `${emitExpr(expr.left)} ${expr.operator} ${emitExpr(expr.right)}`;
        case 'BinaryExpression':
            return `(${emitExpr(expr.left)} ${expr.operator} ${emitExpr(expr.right)})`;
        case 'LogicalExpression':
            return `(${emitExpr(expr.left)} ${expr.operator} ${emitExpr(expr.right)})`;
        case 'UnaryExpression':
            if (expr.prefix) {
                const space = expr.operator.length > 1 ? ' ' : ''; // 'typeof x' vs '!x'
                return `${expr.operator}${space}${emitExpr(expr.argument)}`;
            }
            return `${emitExpr(expr.argument)}${expr.operator}`;
        case 'UpdateExpression':
            if (expr.prefix)
                return `${expr.operator}${emitExpr(expr.argument)}`;
            return `${emitExpr(expr.argument)}${expr.operator}`;
        case 'ConditionalExpression':
            return `(${emitExpr(expr.test)} ? ${emitExpr(expr.consequent)} : ${emitExpr(expr.alternate)})`;
        case 'CallExpression': {
            const callee = emitExpr(expr.callee);
            const args = expr.arguments.map(emitExpr).join(', ');
            return `${callee}(${args})`;
        }
        case 'MemberExpression': {
            const obj = emitExpr(expr.object);
            if (expr.computed) {
                const opt = expr.optional ? '?.' : '';
                return `${obj}${opt}[${emitExpr(expr.property)}]`;
            }
            const dot = expr.optional ? '?.' : '.';
            return `${obj}${dot}${emitExpr(expr.property)}`;
        }
        case 'AwaitExpression':
            return `await ${emitExpr(expr.argument)}`;
        case 'NewExpression': {
            const callee = emitExpr(expr.callee);
            const args = expr.arguments.map(emitExpr).join(', ');
            return `new ${callee}(${args})`;
        }
        case 'TypeAssertion':
            return `${emitExpr(expr.expression)} as ${emitType(expr.assertedType)}`;
        case 'SequenceExpression':
            return expr.expressions.map(emitExpr).join(', ');
        default:
            return `/* unknown expr: ${expr.kind} */`;
    }
}
//# sourceMappingURL=fil-emitter.js.map