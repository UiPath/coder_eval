"use strict";
// AST node type definitions for the FIL compiler
Object.defineProperty(exports, "__esModule", { value: true });
exports.typeToString = typeToString;
exports.isPromiseType = isPromiseType;
exports.isArrayType = isArrayType;
exports.unwrapPromise = unwrapPromise;
exports.isExpression = isExpression;
exports.isStatement = isStatement;
function typeToString(t) {
    if (typeof t === 'string')
        return t;
    if (t.kind === 'Promise')
        return `Promise<${typeToString(t.inner)}>`;
    if (t.kind === 'array')
        return `${typeToString(t.element)}[]`;
    return 'unknown';
}
function isPromiseType(t) {
    return typeof t === 'object' && t.kind === 'Promise';
}
function isArrayType(t) {
    return typeof t === 'object' && t.kind === 'array';
}
function unwrapPromise(t) {
    if (isPromiseType(t))
        return t.inner;
    return t;
}
// Helper type guards
function isExpression(node) {
    return [
        'Identifier', 'Literal', 'TemplateLiteral', 'ArrayExpression', 'ObjectExpression',
        'SpreadElement', 'AssignmentExpression', 'BinaryExpression', 'LogicalExpression',
        'UnaryExpression', 'UpdateExpression', 'ConditionalExpression', 'CallExpression',
        'MemberExpression', 'AwaitExpression', 'NewExpression', 'TypeAssertion', 'SequenceExpression'
    ].includes(node.kind);
}
function isStatement(node) {
    return [
        'BlockStatement', 'VariableDeclaration', 'ExpressionStatement', 'ReturnStatement',
        'ThrowStatement', 'IfStatement', 'WhileStatement', 'ForStatement', 'ForOfStatement',
        'ForInStatement', 'SwitchStatement', 'TryStatement', 'BreakStatement', 'ContinueStatement',
        'LabeledStatement'
    ].includes(node.kind);
}
//# sourceMappingURL=ast.js.map