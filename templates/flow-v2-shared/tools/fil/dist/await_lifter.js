"use strict";
// Await Lifter - transforms async functions into state machines
// Performs liveness analysis and builds frame layout for each async function
Object.defineProperty(exports, "__esModule", { value: true });
exports.AwaitLifter = exports.FRAME_HEADER = void 0;
// Frame header offsets (fixed, same for all async functions)
exports.FRAME_HEADER = {
    func_idx: 0, // i32
    state: 4, // i32
    error_code: 8, // i32
    result_ptr: 12, // i32
    exception_ptr: 16, // i32
    catch_state: 20, // i32
    finally_state: 24, // i32
    parent_frame: 28, // i32
    callee_frame: 32, // i32
    return_ptr: 36, // i32
    HEADER_SIZE: 40,
};
class AwaitLifter {
    constructor() {
        this.asyncFunctions = new Map();
        this.funcIdxMap = new Map();
        this.nextFuncIdx = 0;
    }
    transform(program, functions) {
        // Assign func indices for ALL functions (for call_indirect table)
        // Only async functions go in the resume table
        const asyncFns = program.functions.filter(f => f.isAsync);
        for (let i = 0; i < asyncFns.length; i++) {
            this.funcIdxMap.set(asyncFns[i].name, i);
        }
        this.nextFuncIdx = asyncFns.length;
        // Process each async function
        for (const fn of asyncFns) {
            const info = this.processAsyncFunction(fn, functions);
            this.asyncFunctions.set(fn.name, info);
        }
        return this.asyncFunctions;
    }
    processAsyncFunction(fn, functions) {
        const funcIdx = this.funcIdxMap.get(fn.name) ?? 0;
        // Count await expressions to determine number of states
        const awaitCount = this.countAwaits(fn.body);
        const numStates = awaitCount + 1;
        // Perform liveness analysis to find spilled locals
        const spilledVars = this.findSpilledLocals(fn);
        // Build frame layout
        const slots = [];
        let offset = exports.FRAME_HEADER.HEADER_SIZE;
        // Parameters come first at offset 40+
        for (const param of fn.params) {
            slots.push({ name: param.name, offset, type: param.type, isParam: true });
            offset += 4; // All types use 4 bytes in the frame (pointers for strings/json, values for i32/bool)
        }
        // Then spilled locals
        for (const [name, type] of spilledVars) {
            // Skip if already added as parameter
            if (!slots.find(s => s.name === name)) {
                slots.push({ name, offset, type, isParam: false });
                offset += 4;
            }
        }
        // Align to 4 bytes
        offset = (offset + 3) & ~3;
        const frameLayout = {
            funcName: fn.name,
            funcIdx,
            frameSize: offset,
            slots,
        };
        return {
            funcName: fn.name,
            funcIdx,
            frameLayout,
            numStates,
            fn,
        };
    }
    countAwaits(node) {
        if (!node)
            return 0;
        let count = 0;
        const walk = (n) => {
            if (n.kind === 'AwaitExpression') {
                count++;
                walk(n.argument);
                return;
            }
            // Walk children
            this.walkNode(n, walk);
        };
        walk(node);
        return count;
    }
    walkNode(node, visitor) {
        switch (node.kind) {
            case 'BlockStatement': {
                const b = node;
                for (const s of b.body)
                    visitor(s);
                break;
            }
            case 'VariableDeclaration': {
                const v = node;
                if (v.initializer)
                    visitor(v.initializer);
                break;
            }
            case 'ExpressionStatement':
                visitor(node.expression);
                break;
            case 'ReturnStatement': {
                const r = node;
                if (r.argument)
                    visitor(r.argument);
                break;
            }
            case 'ThrowStatement':
                visitor(node.argument);
                break;
            case 'IfStatement': {
                const i = node;
                visitor(i.test);
                visitor(i.consequent);
                if (i.alternate)
                    visitor(i.alternate);
                break;
            }
            case 'WhileStatement': {
                const w = node;
                visitor(w.test);
                visitor(w.body);
                break;
            }
            case 'ForStatement': {
                const f = node;
                if (f.init)
                    visitor(f.init);
                if (f.test)
                    visitor(f.test);
                if (f.update)
                    visitor(f.update);
                visitor(f.body);
                break;
            }
            case 'ForOfStatement': {
                const f = node;
                visitor(f.iterable);
                visitor(f.body);
                break;
            }
            case 'ForInStatement': {
                const f = node;
                visitor(f.object);
                visitor(f.body);
                break;
            }
            case 'SwitchStatement': {
                const s = node;
                visitor(s.discriminant);
                for (const c of s.cases) {
                    if (c.test)
                        visitor(c.test);
                    for (const st of c.consequent)
                        visitor(st);
                }
                break;
            }
            case 'TryStatement': {
                const t = node;
                visitor(t.block);
                if (t.handler) {
                    visitor(t.handler.body);
                }
                if (t.finalizer)
                    visitor(t.finalizer);
                break;
            }
            case 'LabeledStatement':
                visitor(node.body);
                break;
            case 'BinaryExpression': {
                const b = node;
                visitor(b.left);
                visitor(b.right);
                break;
            }
            case 'LogicalExpression': {
                const l = node;
                visitor(l.left);
                visitor(l.right);
                break;
            }
            case 'UnaryExpression':
                visitor(node.argument);
                break;
            case 'UpdateExpression':
                visitor(node.argument);
                break;
            case 'ConditionalExpression': {
                const c = node;
                visitor(c.test);
                visitor(c.consequent);
                visitor(c.alternate);
                break;
            }
            case 'AssignmentExpression': {
                const a = node;
                visitor(a.left);
                visitor(a.right);
                break;
            }
            case 'CallExpression': {
                const c = node;
                visitor(c.callee);
                for (const a of c.arguments)
                    visitor(a);
                break;
            }
            case 'MemberExpression': {
                const m = node;
                visitor(m.object);
                if (m.computed)
                    visitor(m.property);
                break;
            }
            case 'AwaitExpression':
                visitor(node.argument);
                break;
            case 'NewExpression': {
                const n = node;
                visitor(n.callee);
                for (const a of n.arguments)
                    visitor(a);
                break;
            }
            case 'TypeAssertion':
                visitor(node.expression);
                break;
            case 'TemplateLiteral': {
                const t = node;
                for (const e of t.expressions)
                    visitor(e);
                break;
            }
            case 'ArrayExpression': {
                const a = node;
                for (const e of a.elements)
                    visitor(e);
                break;
            }
            case 'ObjectExpression': {
                const o = node;
                for (const p of o.properties)
                    visitor(p.value);
                break;
            }
            case 'SpreadElement':
                visitor(node.argument);
                break;
            case 'SequenceExpression': {
                const s = node;
                for (const e of s.expressions)
                    visitor(e);
                break;
            }
        }
    }
    // Find all local variables that are "live across" an await point
    // i.e., declared before an await and used after one
    // For simplicity, we spill ALL locals and parameters of async functions
    findSpilledLocals(fn) {
        const locals = new Map();
        // Collect all variable declarations
        const collectDecls = (node) => {
            if (node.kind === 'VariableDeclaration') {
                const v = node;
                const type = v.filType ?? v.typeAnnotation ?? 'json';
                locals.set(v.name, type);
            }
            this.walkNode(node, collectDecls);
        };
        collectDecls(fn.body);
        return locals;
    }
    getStateMachines() {
        return this.asyncFunctions;
    }
    getFuncIdx(name) {
        return this.funcIdxMap.get(name) ?? -1;
    }
    getSlotOffset(info, varName) {
        const slot = info.frameLayout.slots.find(s => s.name === varName);
        if (!slot)
            return -1;
        return slot.offset;
    }
}
exports.AwaitLifter = AwaitLifter;
//# sourceMappingURL=await_lifter.js.map