"use strict";
/**
 * Converts a FIL AST (Program) back to a FlowFile.
 *
 * Strategy:
 * 1. Identify main function → top-level flow
 * 2. Other async functions → subflows
 * 3. Walk each function body generating nodes and edges:
 *    - await executeNode(name, input) → node + edge
 *    - if/else → decision + branches + merge
 *    - for...of → loop (future)
 *    - try/catch → subflow with error port (future)
 *    - return → end node
 * 4. Auto-layout positions on a grid
 */
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
exports.filToFlow = filToFlow;
exports.filToFlowWithScope = filToFlowWithScope;
exports.convertFILExprToFlowExpr = convertFILExprToFlowExpr;
const AST = __importStar(require("fil-compiler/dist/ast"));
const v1_to_v2_1 = require("v1-to-v2");
const expression_1 = require("./expression");
const KNOWN_SCRIPT_KEYS = new Set(['script_id', 'description', 'label']);
/**
 * Extract magic-comment metadata from a statement's leadingComments. Returns
 * `undefined` (cheap) when the statement carries no magic comments at all.
 */
function readScriptMeta(stmt) {
    const cs = stmt.leadingComments;
    if (!cs)
        return undefined;
    let meta;
    for (const c of cs) {
        if (!c.isMagic || !c.key || !KNOWN_SCRIPT_KEYS.has(c.key))
            continue;
        if (!meta)
            meta = {};
        switch (c.key) {
            case 'script_id':
                meta.scriptId = c.value;
                break;
            case 'description':
                meta.description = c.value;
                break;
            case 'label':
                meta.label = c.value;
                break;
        }
    }
    return meta;
}
/** True iff the statement carries any magic comment — used by the
 *  coalesce-pass opt-out. */
function hasAnyMagic(stmt) {
    if (!stmt)
        return false;
    const cs = stmt.leadingComments;
    if (!cs)
        return false;
    return cs.some(c => c.isMagic && c.key && KNOWN_SCRIPT_KEYS.has(c.key));
}
/** Non-magic comments on a statement, in source order. */
function plainComments(stmt) {
    if (!stmt)
        return [];
    const cs = stmt.leadingComments;
    if (!cs)
        return [];
    return cs.filter(c => !c.isMagic);
}
const NODE_WIDTH = 96;
const NODE_HEIGHT = 96;
const X_SPACING = 176;
const Y_SPACING = 176;
const START_X = 250;
const START_Y = 150;
function filToFlow(program, flowId, overrides) {
    const converter = new FilToFlowConverter(program, flowId, overrides);
    return converter.convert();
}
/**
 * Like `filToFlow`, but also returns the scope built for `main()`. Useful
 * when a caller wants to wire FIL expressions back into v1 expressions
 * (Phase A) using the same name-to-path mapping the converter used.
 */
function filToFlowWithScope(program, flowId, overrides) {
    const converter = new FilToFlowConverter(program, flowId, overrides);
    const flow = converter.convert();
    return { flow, scope: converter.getScope() };
}
class FilToFlowConverter {
    constructor(program, flowId, overrides) {
        this.subflows = {};
        /**
         * Map from `action <name>` declared at top level → effective protocol id
         * (the `id` field in the body when present, otherwise the identifier name).
         * Used to resolve `executeNode(<ident>, …)` calls back to a node id, so
         * the converter sees a string name regardless of which call form the FIL
         * author used.
         */
        this.actionIds = new Map();
        /**
         * Scope built while walking `main()`. FIL local name → v1 path
         * (`vars.<name>` for flow globals, `iterator.<name>` for for-of items).
         * Captured for external callers (Phase A wire-up) via `getScope()`.
         */
        this.mainScope = new Map();
        /**
         * Names of FIL locals that MUST be materialized as flow variables — either
         * because they're mutated somewhere (`x = ...`, `x++`) or because they're
         * referenced as a bare identifier in a context that expects a flow variable
         * (decision `expression`, for-of `collection`, async-helper subflow `inputs`).
         * Computed once during `convert()` by scanning the entire program; consulted
         * by `walkVariableDeclaration` to decide whether a `const X = <pure_expr>`
         * can be inlined as an alias instead of getting a setvar script node.
         */
        this.namesRequiringFlowVar = new Set();
        this.program = program;
        this.flowId = overrides?.flowId || flowId || 'converted-flow';
        this.overrides = overrides;
        this.helperFunctions = new Map();
        this.scriptHelpers = new Map();
        for (const fn of program.functions) {
            if (fn.name === 'main')
                continue;
            // Sync functions named `__script_<id>` are inlined script bodies, not
            // subflow helpers — keep them in a separate map so they don't show
            // up as `core.subflow` nodes in the rebuilt v1.
            if (!fn.isAsync && fn.name.startsWith(v1_to_v2_1.SCRIPT_FN_PREFIX)) {
                this.scriptHelpers.set(fn.name, fn);
            }
            else {
                this.helperFunctions.set(fn.name, fn);
            }
        }
        // Index every action declaration → effective protocol id. The
        // `extractExecuteNodeCall` helper consults this map to resolve the
        // identifier form of `executeNode(<ident>, …)`.
        for (const a of program.actions) {
            this.actionIds.set(a.name, resolveActionProtocolId(a));
        }
    }
    /** Snapshot of the scope captured when `main()` was walked. */
    getScope() {
        return new Map(this.mainScope);
    }
    convert() {
        const mainFn = this.program.functions.find(f => f.name === 'main');
        if (!mainFn) {
            throw new Error('FIL program must have a main function');
        }
        // Identify locals that must end up as flow variables before walking the
        // body. The walker uses this to decide whether `const X = <pure_expr>`
        // can be inlined as an expression alias or must emit a setvar node.
        this.namesRequiringFlowVar = collectNamesRequiringFlowVar(this.program);
        // Process helper functions as subflows
        for (const [name, fn] of this.helperFunctions) {
            this.subflows[name] = this.convertFunctionToSubflow(fn);
        }
        // Convert main function
        const ctx = new ConversionContext();
        // Resolve the trigger node id. If the FIL declares any `trigger`
        // entries, the first one's effective id becomes the entry-point id.
        // Otherwise fall back to the conventional `start`.
        const triggerDecl = this.program.triggers[0];
        const triggerId = triggerDecl ? resolveActionProtocolId(triggerDecl) : 'start';
        const triggerNode = makeTriggerNode(triggerId, START_X, START_Y);
        this.applyNodeOverride(triggerNode);
        ctx.addNode(triggerNode);
        // Convert main body
        const variables = this.convertFunctionBody(mainFn, ctx, triggerId);
        // Rewrite post-loop continuation edges from `output` to `success` (see
        // method docstring). Must run after the entire main walk completes
        // because the outer caller of walkForOfStatement adds the post-loop edge
        // when it sees the next statement.
        this.rewriteLoopOutputToSuccess(ctx);
        const flow = {
            id: this.flowId,
            version: this.overrides?.flowVersion || '1.0.0',
            name: this.overrides?.flowName || this.flowId,
            nodes: ctx.nodes,
            edges: ctx.edges,
            variables: this.overrides?.variableOverrides || variables,
            metadata: this.overrides?.metadata || {
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
            },
        };
        // Restore definitions and bindings from FlowOverrides
        if (this.overrides?.definitions && this.overrides.definitions.length > 0) {
            flow.definitions = this.overrides.definitions;
        }
        if (this.overrides?.bindings && this.overrides.bindings.length > 0) {
            flow.bindings = this.overrides.bindings;
        }
        // Restore extra identity fields
        if (this.overrides?.solutionId)
            flow.solutionId = this.overrides.solutionId;
        if (this.overrides?.projectId)
            flow.projectId = this.overrides.projectId;
        if (Object.keys(this.subflows).length > 0) {
            flow.subflows = this.subflows;
        }
        // Post-pass: edges defaulted to sourcePort='output', but some node types
        // (core.action.http, core.action.transform, …) declare their source
        // handles differently in the definition (branch-*, default, error, …).
        // Patch each edge whose source port isn't a valid handle on the source
        // node's definition.
        if (flow.definitions && flow.definitions.length > 0) {
            patchEdgePorts(flow);
        }
        return flow;
    }
    convertFunctionToSubflow(fn) {
        const ctx = new ConversionContext();
        const startId = `${fn.name}Start`;
        ctx.addNode(makeTriggerNode(startId, START_X, START_Y));
        const variables = this.convertFunctionBody(fn, ctx, startId);
        this.rewriteLoopOutputToSuccess(ctx);
        return {
            nodes: ctx.nodes,
            edges: ctx.edges,
            variables,
        };
    }
    convertFunctionBody(fn, ctx, lastNodeId) {
        const globals = [];
        const nodeVars = [];
        const variableUpdates = {};
        const scope = new Map();
        // Map function params to input variables, and seed the scope with each
        // param resolving to `vars.<name>` (v1 binds function inputs to flow
        // globals).
        for (const param of fn.params) {
            globals.push({
                id: param.name,
                direction: 'in',
                type: filTypeToFlow(param.type),
            });
            scope.set(param.name, `vars.${param.name}`);
        }
        // Walk statements
        const walkResult = this.walkStatements(fn.body.body, ctx, lastNodeId, globals, nodeVars, variableUpdates, scope);
        // Capture main's scope for callers that want to use it (Phase A wire-up).
        if (fn.name === 'main') {
            this.mainScope = scope;
        }
        // Check for return type → output variable
        const retType = AST.isPromiseType(fn.returnType)
            ? AST.unwrapPromise(fn.returnType)
            : fn.returnType;
        if (retType !== 'void') {
            // Output variables are detected from return statements during walk
        }
        // Collapse contiguous runs of `setvar_X` const-init script nodes into a
        // single script that returns an object, with one variableUpdate per var.
        coalesceConstScriptChains(ctx, nodeVars, variableUpdates);
        const vars = {};
        if (globals.length > 0)
            vars.globals = globals;
        if (nodeVars.length > 0)
            vars.nodes = nodeVars;
        if (Object.keys(variableUpdates).length > 0)
            vars.variableUpdates = variableUpdates;
        return vars;
    }
    /**
     * Walk a list of statements, creating nodes and edges.
     * Returns the ID of the last node created (for chaining edges).
     */
    walkStatements(stmts, ctx, lastNodeId, globals, nodeVars, variableUpdates, scope) {
        let prevNodeId = lastNodeId;
        for (const stmt of stmts) {
            prevNodeId = this.walkStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
        }
        return prevNodeId;
    }
    walkStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope) {
        switch (stmt.kind) {
            case 'ExpressionStatement':
                return this.walkExpressionStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
            case 'VariableDeclaration':
                return this.walkVariableDeclaration(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
            case 'IfStatement':
                return this.walkIfStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
            case 'ReturnStatement':
                return this.walkReturnStatement(stmt, ctx, prevNodeId, globals, nodeVars);
            case 'ForOfStatement':
                return this.walkForOfStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
            case 'BlockStatement':
                return this.walkStatements(stmt.body, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope);
            default:
                // Skip unhandled statement types
                return prevNodeId;
        }
    }
    walkExpressionStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope) {
        const expr = stmt.expression;
        // Check for await executeNode(...)
        const execCall = extractExecuteNodeCall(expr, this.actionIds);
        if (execCall) {
            const { nodeName, inputExpr } = execCall;
            const node = makeActionNode(nodeName, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeName, 'input');
            return nodeName;
        }
        // Check for await Promise.all([...]) / Promise.any([...]) → fan-out + merge
        const fanout = extractPromiseFanoutCall(expr, this.actionIds);
        if (fanout) {
            return this.emitPromiseFanout(fanout, ctx, prevNodeId);
        }
        // Check for await executeTimer(...) → core.logic.delay
        const timerCall = extractExecuteTimerCall(expr);
        if (timerCall) {
            const nodeId = ctx.generateId('delay');
            const node = makeDelayNode(nodeId, timerCall, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeId, 'input');
            return nodeId;
        }
        // Check for sync `__script_<id>()` call → core.action.script
        const scriptCall = extractScriptCall(expr, this.scriptHelpers);
        if (scriptCall) {
            const { nodeId, fn } = scriptCall;
            const body = recoverScriptBody(fn);
            const node = makeScriptNode(nodeId, body, ctx.nextX(), ctx.currentY());
            applyScriptMeta(node, readScriptMeta(fn));
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeId, 'input');
            return nodeId;
        }
        // Check for subflow call: await subflowName(...)
        const subflowCall = extractSubflowCall(expr, this.helperFunctions);
        if (subflowCall) {
            const { fnName } = subflowCall;
            const node = makeSubflowNode(fnName, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', fnName, 'input');
            return fnName;
        }
        // Phase C: mutations (`x = expr`, `x += expr`, `x++`, `++x`, …).
        // Emit a setvar script node that returns the new value, and a
        // variableUpdate that copies the script's result back into the named
        // flow variable. v1 has no in-script variable mutation (scripts run in
        // a sandbox); the framework writes the variable from the script's
        // output via the variableUpdate.
        if (expr.kind === 'AssignmentExpression' || expr.kind === 'UpdateExpression') {
            return this.emitMutation(expr, stmt, ctx, prevNodeId, globals, variableUpdates, scope);
        }
        return prevNodeId;
    }
    /**
     * Emit a fan-out of executeNode/executeTimer entries plus a `core.logic.merge`
     * that joins them. The merge becomes the new "previous node" so downstream
     * statements chain from a single source — the same shape if/else uses.
     *
     * Promise.all and Promise.any produce identical graphs here; the v1 graph
     * doesn't distinguish "wait for all" from "first wins", only the dispatcher
     * does.
     */
    emitPromiseFanout(fanout, ctx, prevNodeId) {
        if (fanout.entries.length === 0)
            return prevNodeId;
        const savedY = ctx.currentY();
        const entryNodeIds = [];
        // Stack the branches vertically around the current Y so the layout reads
        // — same approach as if/else (savedY ± Y_SPACING/2).
        fanout.entries.forEach((entry, i) => {
            const lane = i - (fanout.entries.length - 1) / 2;
            ctx.pushY(savedY + lane * Y_SPACING);
            let nodeId;
            if (entry.kind === 'node') {
                const node = makeActionNode(entry.nodeName, ctx.nextX(), ctx.currentY());
                this.applyNodeOverride(node);
                ctx.addNode(node);
                nodeId = entry.nodeName;
            }
            else {
                nodeId = ctx.generateId('delay');
                const node = makeDelayNode(nodeId, entry.timer, ctx.nextX(), ctx.currentY());
                this.applyNodeOverride(node);
                ctx.addNode(node);
            }
            ctx.addEdge(prevNodeId, 'output', nodeId, 'input');
            entryNodeIds.push(nodeId);
            ctx.popY();
        });
        const mergeId = ctx.generateId('merge');
        const mergeNode = {
            id: mergeId,
            type: v1_to_v2_1.NODE_TYPES.MERGE,
            typeVersion: '1.0.0',
            ui: { position: { x: ctx.nextX(), y: savedY } },
            display: { label: 'Merge' },
        };
        this.applyNodeOverride(mergeNode);
        ctx.addNode(mergeNode);
        for (const id of entryNodeIds) {
            ctx.addEdge(id, 'output', mergeId, 'input');
        }
        return mergeId;
    }
    /**
     * Emit a setvar script node + variableUpdate for a mutation expression.
     * Throws with a clear error if the mutation can't survive in v1.
     */
    emitMutation(expr, sourceStmt, ctx, prevNodeId, globals, variableUpdates, scope) {
        // Resolve the target identifier and the right-hand side.
        let varName;
        let rhs;
        if (expr.kind === 'AssignmentExpression') {
            if (expr.left.kind !== 'Identifier') {
                throw new Error(`Cannot convert mutation to v1: target must be a simple variable ` +
                    `name (got ${expr.left.kind}). v1 flow variables are not deeply ` +
                    `mutable — assigning to a property like \`obj.foo = x\` has no ` +
                    `equivalent. Replace with \`obj = { ...obj, foo: x }\`.`);
            }
            varName = expr.left.name;
            if (expr.operator === '=') {
                rhs = expr.right;
            }
            else {
                // Compound assignment: x op= y → x = x op y
                const baseOp = expr.operator.slice(0, -1); // strip trailing '='
                rhs = {
                    kind: 'BinaryExpression',
                    operator: baseOp,
                    left: { kind: 'Identifier', name: varName },
                    right: expr.right,
                };
            }
        }
        else {
            // UpdateExpression: x++, ++x, x--, --x  →  x = x +/- 1
            if (expr.argument.kind !== 'Identifier') {
                throw new Error(`Cannot convert ${expr.operator} to v1: target must be a simple ` +
                    `variable name (got ${expr.argument.kind}).`);
            }
            varName = expr.argument.name;
            rhs = {
                kind: 'BinaryExpression',
                operator: expr.operator === '++' ? '+' : '-',
                left: { kind: 'Identifier', name: varName },
                right: { kind: 'Literal', value: 1, raw: '1' },
            };
        }
        if (containsAwait(rhs)) {
            throw new Error(`Cannot convert assignment to "${varName}" to v1: right-hand side ` +
                `contains an await expression. Move the await to its own statement ` +
                `(\`const tmp = await ...; ${varName} = tmp;\`) so the engine can ` +
                `execute the await as an action node first.`);
        }
        // The variable should have been declared earlier as a flow global. If
        // it isn't (e.g. agent-written FIL skipped the declaration), declare
        // it implicitly with type 'object' — the framework infers from runtime.
        if (!globals.find((g) => g.id === varName)) {
            globals.push({ id: varName, direction: 'inout', type: 'object' });
        }
        // Make sure the mutated name resolves in scope. Mutations don't change
        // the v1 path — `vars.<name>` either way.
        if (!scope.has(varName))
            scope.set(varName, `vars.${varName}`);
        // Emit the script node. If the source statement carries a magic
        // `// script_id: <name>` comment, honor it; otherwise auto-generate.
        const meta = readScriptMeta(sourceStmt);
        const scriptNodeId = meta?.scriptId
            ? ctx.claimId(meta.scriptId)
            : ctx.generateId(`setvar_${varName}_`);
        const body = `return ${(0, v1_to_v2_1.emitExpression)(rhs)};`;
        const node = makeScriptNode(scriptNodeId, body, ctx.nextX(), ctx.currentY());
        applyScriptMeta(node, meta);
        this.applyNodeOverride(node);
        ctx.addNode(node);
        ctx.stmtForNode.set(scriptNodeId, sourceStmt);
        ctx.addEdge(prevNodeId, 'output', scriptNodeId, 'input');
        // After the script runs, copy its result.response back into the named
        // variable. The expression is `=js:result.response` — evaluated in the
        // node-completion context, where `result` is the just-completed script's
        // output envelope and `result.response` is its return value.
        if (!variableUpdates[scriptNodeId])
            variableUpdates[scriptNodeId] = [];
        variableUpdates[scriptNodeId].push({
            variableId: varName,
            expression: '=js:result.response',
        });
        return scriptNodeId;
    }
    /**
     * Returns true if a local named `name` must be materialized as a flow
     * variable rather than inlined as an expression alias. Conservative: any
     * scan hit forces materialization.
     */
    isLocalMutatedOrUsedAsBareIdentifier(name) {
        return this.namesRequiringFlowVar.has(name);
    }
    walkVariableDeclaration(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope) {
        // Handle 'let' with type annotation — could be inout/output variable
        if (stmt.declarationKind === 'let' && stmt.typeAnnotation) {
            const existingGlobal = globals.find(g => g.id === stmt.name);
            if (!existingGlobal) {
                globals.push({
                    id: stmt.name,
                    direction: 'inout',
                    type: filTypeToFlow(stmt.typeAnnotation),
                    defaultValue: stmt.initializer ? extractLiteralValue(stmt.initializer) : undefined,
                });
            }
            scope.set(stmt.name, `vars.${stmt.name}`);
        }
        if (!stmt.initializer)
            return prevNodeId;
        // `const x = <literal>;` — register a flow global with the literal as
        // its default. Literals don't need a script node; the framework can
        // initialise the variable from `defaultValue` directly.
        if (stmt.initializer.kind === 'Literal') {
            const existingGlobal = globals.find(g => g.id === stmt.name);
            if (!existingGlobal) {
                globals.push({
                    id: stmt.name,
                    direction: 'inout',
                    type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation)
                        : guessTypeFromLiteral(stmt.initializer),
                    defaultValue: extractLiteralValue(stmt.initializer),
                });
            }
            scope.set(stmt.name, `vars.${stmt.name}`);
            return prevNodeId;
        }
        // Check for: const xData = await executeNode("x", ...);
        const execCall = extractExecuteNodeCall(stmt.initializer, this.actionIds);
        if (execCall) {
            const { nodeName } = execCall;
            const node = makeActionNode(nodeName, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeName, 'input');
            // Bind the FIL local name (e.g. `userData`) to the node's output
            // port. This way `vars.userData` resolves to the action's result in
            // any later expression. Without this, an expression referencing
            // `userData` would have no v1 binding.
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId: nodeName, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return nodeName;
        }
        // Check for: const results = await Promise.all([...]) / Promise.any([...])
        const fanoutInit = extractPromiseFanoutCall(stmt.initializer, this.actionIds);
        if (fanoutInit) {
            const mergeId = this.emitPromiseFanout(fanoutInit, ctx, prevNodeId);
            // The fan-out's result is an array of node outputs — there's no single
            // v1 source to bind, so we register the local against the merge node's
            // output port. Downstream `vars.<name>[i]` access through this binding
            // won't survive v1 type-checking, but `vars.<name>` is at least defined.
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId: mergeId, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return mergeId;
        }
        // const fooData = __script_foo();
        const scriptCall = extractScriptCall(stmt.initializer, this.scriptHelpers);
        if (scriptCall) {
            const { nodeId, fn } = scriptCall;
            const body = recoverScriptBody(fn);
            const node = makeScriptNode(nodeId, body, ctx.nextX(), ctx.currentY());
            applyScriptMeta(node, readScriptMeta(fn));
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeId, 'input');
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return nodeId;
        }
        // const fired = await executeTimer(TimeSpan.fromMillis(900000n));
        const timerCall = extractExecuteTimerCall(stmt.initializer);
        if (timerCall) {
            const nodeId = ctx.generateId('delay');
            const node = makeDelayNode(nodeId, timerCall, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', nodeId, 'input');
            // Even though delay returns a value, register the local in scope for
            // expression resolution. The binding-side variable is rare in v1 but
            // a user could still write `vars.fired` in a downstream expression.
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return nodeId;
        }
        // Check for subflow call
        const subflowCall = extractSubflowCall(stmt.initializer, this.helperFunctions);
        if (subflowCall) {
            const { fnName } = subflowCall;
            const node = makeSubflowNode(fnName, ctx.nextX(), ctx.currentY());
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.addEdge(prevNodeId, 'output', fnName, 'input');
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId: fnName, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return fnName;
        }
        // Phase B: `const/let <name> = <expr>` where <expr> is a non-trivial
        // computation (variable refs, arithmetic, calls, …). v1 has no
        // "computed initializer" for globals, so we either:
        //   (a) Inline as a v1 `=js:` expression alias in `scope` — no graph
        //       node emitted. Used when the initializer is `const`, doesn't
        //       contain await, is read-only (no later mutation), and survives
        //       `tryEmitV1Expression`. Downstream consumers (connector
        //       inputs, member access on the alias) substitute the expression
        //       inline at runtime. This avoids `setvar_X_1` clutter for the
        //       common chain pattern (`const x = obj.field as string;`).
        //   (b) Fall back to a `core.action.script` node that returns the
        //       value, wires into the graph at the declaration's position,
        //       and binds the script's output port to a flow variable. Used
        //       when the FIL isn't v1-expressible (DateTime helpers, etc.),
        //       when the local is mutated later, or when it's referenced
        //       from a decision/loop/script body (which use bare identifiers
        //       and need a real flow variable).
        if (stmt.initializer) {
            // Reject things that can't survive as a sync v1 script body.
            if (containsAwait(stmt.initializer)) {
                throw new Error(`Cannot convert const/let "${stmt.name}" to v1: ` +
                    `initializer contains an await expression. Move the await to its ` +
                    `own statement (\`const x = await ...;\`) so the engine can ` +
                    `execute it as an action node.`);
            }
            // Magic comments (`// script_id: …`, `// description: …`, `// label: …`)
            // signal "this declaration should materialize as a named v1 node."
            // Skip path (a) when present so the metadata has a node to attach to.
            const meta = readScriptMeta(stmt);
            // (a) Inline as an expression alias if the local is never mutated and
            // never referenced as a bare identifier from a context that needs a
            // real flow variable (decision test, loop collection, etc.). The
            // declaration keyword doesn't matter — a `let` that's only ever read
            // is just as inlineable as a `const`. The mutation tracker handles
            // both cases; typed `let X: T = …` is preemptively kept as a flow
            // var (see collectNamesRequiringFlowVar) to preserve the
            // output-variable convention.
            if (!meta && !this.isLocalMutatedOrUsedAsBareIdentifier(stmt.name)) {
                const emitted = (0, expression_1.tryEmitV1Expression)(stmt.initializer, scope, { bare: true });
                if (emitted) {
                    // tryEmitV1Expression already wraps composite expressions in
                    // parens, so downstream substitutions bind correctly.
                    scope.set(stmt.name, emitted.expression);
                    return prevNodeId;
                }
            }
            // (b) Fallback: materialize as a setvar script node + flow variable.
            const scriptNodeId = meta?.scriptId
                ? ctx.claimId(meta.scriptId)
                : ctx.generateId(`setvar_${stmt.name}_`);
            const body = `return ${(0, v1_to_v2_1.emitExpression)(stmt.initializer)};`;
            const node = makeScriptNode(scriptNodeId, body, ctx.nextX(), ctx.currentY());
            applyScriptMeta(node, meta);
            this.applyNodeOverride(node);
            ctx.addNode(node);
            ctx.stmtForNode.set(scriptNodeId, stmt);
            ctx.addEdge(prevNodeId, 'output', scriptNodeId, 'input');
            // Register the variable: use the explicit type annotation if present,
            // otherwise leave as 'object' (the framework infers from the script
            // output at runtime).
            if (!globals.find(g => g.id === stmt.name)) {
                globals.push({
                    id: stmt.name,
                    direction: 'inout',
                    type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                });
            }
            // The variable receives its value from the script's output port.
            nodeVars.push({
                id: stmt.name,
                type: stmt.typeAnnotation ? filTypeToFlow(stmt.typeAnnotation) : 'object',
                binding: { nodeId: scriptNodeId, outputId: 'output' },
            });
            scope.set(stmt.name, `vars.${stmt.name}`);
            return scriptNodeId;
        }
        return prevNodeId;
    }
    walkIfStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope) {
        // Create decision node
        const decisionId = ctx.generateId('decision');
        const expression = convertFILExprToFlowExpr(stmt.test);
        const decisionNode = {
            id: decisionId,
            type: v1_to_v2_1.NODE_TYPES.DECISION,
            typeVersion: '1.0.0',
            ui: { position: { x: ctx.nextX(), y: ctx.currentY() } },
            display: { label: 'Decision', icon: 'trending-up-down' },
            inputs: {
                trueLabel: 'True',
                falseLabel: 'False',
                expression,
            },
        };
        this.applyNodeOverride(decisionNode);
        ctx.addNode(decisionNode);
        ctx.addEdge(prevNodeId, 'output', decisionId, 'input');
        // Process true branch
        const trueStatements = stmt.consequent.kind === 'BlockStatement'
            ? stmt.consequent.body
            : [stmt.consequent];
        const savedY = ctx.currentY();
        // True branch (upper). v1 hoists block-scoped variables to the function
        // level — both arms share the outer scope.
        ctx.pushY(savedY - Y_SPACING / 2);
        const trueEnd = this.walkStatements(trueStatements, ctx, decisionId, globals, nodeVars, variableUpdates, scope);
        // Fix: the edge from decision should use 'true' port
        // Remove the auto-created edge and replace with correct port
        const lastTrueEdge = ctx.edges.find(e => e.sourceNodeId === decisionId && e.targetPort === 'input'
            && e.sourcePort === 'output');
        if (lastTrueEdge) {
            lastTrueEdge.sourcePort = 'true';
            lastTrueEdge.id = `edge_${decisionId}-true-${lastTrueEdge.targetNodeId}-input`;
        }
        ctx.popY();
        // False branch (lower)
        let falseEnd;
        if (stmt.alternate) {
            const falseStatements = stmt.alternate.kind === 'BlockStatement'
                ? stmt.alternate.body
                : [stmt.alternate];
            ctx.pushY(savedY + Y_SPACING / 2);
            falseEnd = this.walkStatements(falseStatements, ctx, decisionId, globals, nodeVars, variableUpdates, scope);
            // Fix edge port to 'false'
            const lastFalseEdge = ctx.edges.find(e => e.sourceNodeId === decisionId && e.targetPort === 'input'
                && e.sourcePort === 'output');
            if (lastFalseEdge) {
                lastFalseEdge.sourcePort = 'false';
                lastFalseEdge.id = `edge_${decisionId}-false-${lastFalseEdge.targetNodeId}-input`;
            }
            ctx.popY();
        }
        // Check if both branches end with return/end — if so, no merge needed
        const trueEndsWithReturn = endsWithReturn(trueStatements);
        const falseEndsWithReturn = stmt.alternate
            ? endsWithReturn(stmt.alternate.kind === 'BlockStatement' ? stmt.alternate.body : [stmt.alternate])
            : false;
        if (trueEndsWithReturn && falseEndsWithReturn) {
            // No merge needed — branches terminate independently
            return decisionId; // Return decision as "last" (nothing follows)
        }
        // Create merge node
        const mergeId = ctx.generateId('merge');
        const mergeNode = {
            id: mergeId,
            type: v1_to_v2_1.NODE_TYPES.MERGE,
            typeVersion: '1.0.0',
            ui: { position: { x: ctx.nextX(), y: savedY } },
            display: { label: 'Merge' },
        };
        this.applyNodeOverride(mergeNode);
        ctx.addNode(mergeNode);
        // Connect branches to merge
        if (trueEnd && trueEnd !== decisionId && !trueEndsWithReturn) {
            ctx.addEdge(trueEnd, 'output', mergeId, 'input');
        }
        else if (!trueEndsWithReturn) {
            ctx.addEdge(decisionId, 'true', mergeId, 'input');
        }
        if (falseEnd && falseEnd !== decisionId && !falseEndsWithReturn) {
            ctx.addEdge(falseEnd, 'output', mergeId, 'input');
        }
        else if (stmt.alternate && !falseEndsWithReturn) {
            ctx.addEdge(decisionId, 'false', mergeId, 'input');
        }
        else if (!stmt.alternate) {
            // No else branch — false goes directly to merge
            ctx.addEdge(decisionId, 'false', mergeId, 'input');
        }
        return mergeId;
    }
    walkReturnStatement(stmt, ctx, prevNodeId, globals, nodeVars) {
        if (!stmt.argument) {
            const endId = ctx.generateId('end');
            const endNode = {
                id: endId,
                type: v1_to_v2_1.NODE_TYPES.END,
                typeVersion: '1.0.0',
                ui: { position: { x: ctx.nextX(), y: ctx.currentY() } },
                display: { label: 'End', icon: 'circle-check', shape: 'circle' },
            };
            this.applyNodeOverride(endNode);
            ctx.addNode(endNode);
            ctx.addEdge(prevNodeId, 'output', endId, 'input');
            return endId;
        }
        // Create end node with outputs
        const endId = ctx.generateId('end');
        const outputs = {};
        if (stmt.argument.kind === 'ObjectExpression') {
            // Return { a: expr1, b: expr2 } → end node with multiple outputs
            for (const prop of stmt.argument.properties) {
                outputs[prop.key] = { source: `=js:${convertFILExprToFlowExpr(prop.value)}` };
                // Also register as output variable if not already present
                if (!globals.find(g => g.id === prop.key && g.direction === 'out')) {
                    // Change existing inout to out, or add new out
                    const existing = globals.find(g => g.id === prop.key);
                    if (existing) {
                        existing.direction = 'out';
                    }
                    else {
                        globals.push({ id: prop.key, direction: 'out', type: 'object' });
                    }
                }
            }
        }
        else {
            // Single return value
            const source = `=js:${convertFILExprToFlowExpr(stmt.argument)}`;
            // Find an existing out variable, or promote an inout, or create one
            let outVar = globals.find(g => g.direction === 'out');
            if (!outVar) {
                // If returning an identifier that matches a declared inout var, promote it
                if (stmt.argument.kind === 'Identifier') {
                    const argName = stmt.argument.name;
                    const inoutVar = globals.find(g => g.id === argName && g.direction === 'inout');
                    if (inoutVar) {
                        inoutVar.direction = 'out';
                        outVar = inoutVar;
                    }
                }
                // If still no out var, promote the first inout or create a new one
                if (!outVar) {
                    const firstInout = globals.find(g => g.direction === 'inout');
                    if (firstInout) {
                        firstInout.direction = 'out';
                        outVar = firstInout;
                    }
                    else {
                        const newVar = { id: 'result', direction: 'out', type: 'object' };
                        globals.push(newVar);
                        outVar = newVar;
                    }
                }
            }
            outputs[outVar.id] = { source };
        }
        const endNode = {
            id: endId,
            type: v1_to_v2_1.NODE_TYPES.END,
            typeVersion: '1.0.0',
            ui: { position: { x: ctx.nextX(), y: ctx.currentY() } },
            display: { label: 'End', icon: 'circle-check', shape: 'circle' },
            outputs,
        };
        this.applyNodeOverride(endNode);
        ctx.addNode(endNode);
        ctx.addEdge(prevNodeId, 'output', endId, 'input');
        return endId;
    }
    walkForOfStatement(stmt, ctx, prevNodeId, globals, nodeVars, variableUpdates, scope) {
        // Create the loop node and wire entry.
        const loopId = ctx.generateId('loop');
        const collection = convertFILExprToFlowExpr(stmt.iterable);
        const loopNode = {
            id: loopId,
            type: v1_to_v2_1.NODE_TYPES.LOOP,
            typeVersion: '1.0.0',
            ui: { position: { x: ctx.nextX(), y: ctx.currentY() } },
            display: { label: 'Loop' },
            inputs: { collection },
        };
        this.applyNodeOverride(loopNode);
        ctx.addNode(loopNode);
        ctx.addEdge(prevNodeId, 'output', loopId, 'input');
        // Walk the body INLINE — body nodes live in the same flow as the rest.
        // The body inherits the outer scope and adds the iterator variable as
        // `iterator.<name>` (the v1 namespace for for-of items). The first body
        // statement adds an edge `loopId.output → firstBody.input` (loop's
        // body-entry port); the body terminus is wired back as
        // `terminus.output → loopId.loopBack` (patchEdgePorts fixes the source
        // port if the terminus's node type doesn't accept `output`).
        const bodyStatements = stmt.body.kind === 'BlockStatement'
            ? stmt.body.body
            : [stmt.body];
        if (bodyStatements.length === 0)
            return loopId;
        const bodyScope = new Map(scope);
        bodyScope.set(stmt.item, `iterator.${stmt.item}`);
        const edgesBefore = ctx.edges.length;
        const bodyTerminus = this.walkStatements(bodyStatements, ctx, loopId, globals, nodeVars, variableUpdates, bodyScope);
        // Find the body-entry node (target of the first new edge from loopId).
        const bodyEntryEdge = ctx.edges.slice(edgesBefore).find(e => e.sourceNodeId === loopId && e.sourcePort === 'output');
        if (bodyEntryEdge) {
            ctx.loopBodyEntries.set(loopId, bodyEntryEdge.targetNodeId);
            // Wire body terminus → loop.loopBack (back to the loop for the next
            // iteration). For single-statement bodies, terminus === bodyEntry,
            // which is fine — the loopBack edge is what matters.
            if (bodyTerminus !== loopId) {
                ctx.addEdge(bodyTerminus, 'output', loopId, 'loopBack');
            }
        }
        return loopId;
    }
    /**
     * After main walks complete, post-loop continuation edges added by the
     * outer walkStatement chain look like `loop.output → next.input` — but
     * `output` is the v1 loop's body-entry port. Rewrite those edges to use
     * `success` (the post-loop continuation port). The body-entry edge
     * (recorded in {@link ConversionContext.loopBodyEntries}) keeps `output`.
     */
    rewriteLoopOutputToSuccess(ctx) {
        for (const [loopId, bodyEntryId] of ctx.loopBodyEntries) {
            for (const edge of ctx.edges) {
                if (edge.sourceNodeId === loopId
                    && edge.sourcePort === 'output'
                    && edge.targetNodeId !== bodyEntryId) {
                    const oldId = edge.id;
                    edge.sourcePort = 'success';
                    edge.id = oldId.replace('-output-', '-success-');
                }
            }
        }
    }
    /**
     * Apply node overrides from FlowOverrides to a generated node.
     * Replaces auto-generated display, ui, model, type, and inputs with the original values.
     */
    applyNodeOverride(node) {
        if (!this.overrides)
            return;
        const override = this.overrides.nodeOverrides[node.id];
        if (!override)
            return;
        if (override.type)
            node.type = override.type;
        if (override.typeVersion)
            node.typeVersion = override.typeVersion;
        if (override.display)
            node.display = { ...node.display, ...override.display };
        if (override.ui) {
            if (override.ui.position)
                node.ui.position = override.ui.position;
            if (override.ui.size)
                node.ui.size = override.ui.size;
            if (override.ui.collapsed !== undefined)
                node.ui.collapsed = override.ui.collapsed;
        }
        if (override.model)
            node.model = override.model;
        if (override.inputs)
            node.inputs = override.inputs;
        if (override.outputs)
            node.outputs = override.outputs;
    }
}
// ─── Conversion context ───────────────────────────────────────────────────────
class ConversionContext {
    constructor() {
        this.nodes = [];
        this.edges = [];
        /**
         * Loop nodes (`core.logic.loop`) whose body was inlined into this context.
         * Maps loopId → the first body node's id. Used by the post-walk pass that
         * rewrites post-loop continuation edges to use the `success` port instead
         * of `output` (the body-entry port).
         */
        this.loopBodyEntries = new Map();
        this.x = START_X;
        this.yStack = [START_Y];
        this.idCounters = new Map();
        /**
         * Track node ids we've already emitted so multi-branch flows that converge
         * on the same node (e.g. an `if/else if/else` chain whose arms each reach
         * the same downstream `mock2`) emit the node body once. The edge from
         * each branch still emits — that's how the convergence is recorded.
         */
        this.emittedNodeIds = new Set();
        /** Mirror for edges so we don't emit `edge_A-output-B-input` twice. */
        this.emittedEdgeIds = new Set();
        /**
         * For every node we minted from a FIL statement, remember the statement so
         * downstream passes (coalesce, trivia emission) can read its
         * `leadingComments` without re-walking the AST.
         */
        this.stmtForNode = new Map();
        /** Ids reserved via `claimId` — prevents duplicate `script_id` magic comments. */
        this.claimedIds = new Set();
    }
    nextX() {
        const cur = this.x;
        this.x += X_SPACING;
        return cur;
    }
    currentY() {
        return this.yStack[this.yStack.length - 1];
    }
    pushY(y) {
        this.yStack.push(y);
    }
    popY() {
        if (this.yStack.length > 1)
            this.yStack.pop();
    }
    /** Has this node id already been emitted? Callers use this to skip
     *  re-emitting bodies during walks that revisit converged nodes. */
    hasNode(id) {
        return this.emittedNodeIds.has(id);
    }
    addNode(node) {
        if (this.emittedNodeIds.has(node.id))
            return;
        this.emittedNodeIds.add(node.id);
        this.nodes.push(node);
    }
    addEdge(sourceNodeId, sourcePort, targetNodeId, targetPort) {
        const id = `edge_${sourceNodeId}-${sourcePort}-${targetNodeId}-${targetPort}`;
        if (this.emittedEdgeIds.has(id))
            return;
        this.emittedEdgeIds.add(id);
        this.edges.push({ id, sourceNodeId, sourcePort, targetNodeId, targetPort });
    }
    generateId(prefix) {
        const count = (this.idCounters.get(prefix) || 0) + 1;
        this.idCounters.set(prefix, count);
        return `${prefix}${count}`;
    }
    /**
     * Reserve a specific node id — used when a `// script_id: <name>` magic
     * comment names the node explicitly. Throws on duplicate so two FIL
     * statements can't fight over the same v1 id. If the requested id matches
     * the `setvar_<base>_<n>` shape, bump the corresponding counter so a later
     * `generateId('setvar_<base>_')` doesn't recycle the same name.
     */
    claimId(id) {
        if (this.claimedIds.has(id)) {
            throw new Error(`Duplicate script_id "${id}": two FIL statements claim the same v1 node id.`);
        }
        this.claimedIds.add(id);
        const m = /^setvar_(.+)_(\d+)$/.exec(id);
        if (m) {
            const key = `setvar_${m[1]}_`;
            const n = Number(m[2]);
            this.idCounters.set(key, Math.max(this.idCounters.get(key) ?? 0, n));
        }
        return id;
    }
}
// ─── Helper functions ─────────────────────────────────────────────────────────
function extractExecuteNodeCall(expr, actionIds) {
    // Match: await executeNode(<ident>, …)  — Phase 1 syntax. The identifier
    // resolves through `actionIds` to the action's protocol id.
    // Also: await executeNode("name", …)    — legacy/string-literal form.
    let callExpr;
    if (expr.kind === 'AwaitExpression' && expr.argument.kind === 'CallExpression') {
        callExpr = expr.argument;
    }
    else if (expr.kind === 'CallExpression') {
        callExpr = expr;
    }
    if (!callExpr)
        return null;
    if (callExpr.callee.kind !== 'Identifier' || callExpr.callee.name !== 'executeNode')
        return null;
    if (callExpr.arguments.length < 1)
        return null;
    const nameArg = callExpr.arguments[0];
    let nodeName;
    if (nameArg.kind === 'Identifier' && actionIds && actionIds.has(nameArg.name)) {
        nodeName = actionIds.get(nameArg.name);
    }
    else if (nameArg.kind === 'Literal' && typeof nameArg.value === 'string') {
        nodeName = nameArg.value;
    }
    else {
        return null;
    }
    return {
        nodeName,
        inputExpr: callExpr.arguments[1],
    };
}
function extractPromiseFanoutCall(expr, actionIds) {
    let callExpr;
    if (expr.kind === 'AwaitExpression' && expr.argument.kind === 'CallExpression') {
        callExpr = expr.argument;
    }
    else if (expr.kind === 'CallExpression') {
        callExpr = expr;
    }
    if (!callExpr)
        return null;
    if (callExpr.callee.kind !== 'MemberExpression')
        return null;
    const callee = callExpr.callee;
    if (callee.object.kind !== 'Identifier' || callee.object.name !== 'Promise')
        return null;
    if (callee.property.kind !== 'Identifier')
        return null;
    const method = callee.property.name;
    if (method !== 'all' && method !== 'any')
        return null;
    if (callExpr.arguments.length !== 1 || callExpr.arguments[0].kind !== 'ArrayExpression') {
        throw new Error(`Promise.${method} expects a single array literal of executeNode/executeTimer calls`);
    }
    const arr = callExpr.arguments[0];
    const entries = [];
    for (const el of arr.elements) {
        const ex = extractExecuteNodeCall(el, actionIds);
        if (ex) {
            entries.push({ kind: 'node', nodeName: ex.nodeName, inputExpr: ex.inputExpr });
            continue;
        }
        const tm = extractExecuteTimerCall(el);
        if (tm) {
            entries.push({ kind: 'timer', timer: tm });
            continue;
        }
        throw new Error(`Promise.${method} entries must be executeNode(...) or executeTimer(...) calls; got ${el.kind}`);
    }
    return { kind: method, entries };
}
/** Resolve an action declaration's effective protocol id. */
function resolveActionProtocolId(decl) {
    if (!decl.fields)
        return decl.name;
    const idProp = decl.fields.properties.find((p) => p.key === 'id');
    if (idProp && idProp.value.kind === 'Literal' && typeof idProp.value.value === 'string') {
        return idProp.value.value;
    }
    return decl.name;
}
function extractSubflowCall(expr, helpers) {
    let callExpr;
    if (expr.kind === 'AwaitExpression' && expr.argument.kind === 'CallExpression') {
        callExpr = expr.argument;
    }
    else if (expr.kind === 'CallExpression') {
        callExpr = expr;
    }
    if (!callExpr)
        return null;
    if (callExpr.callee.kind !== 'Identifier')
        return null;
    if (!helpers.has(callExpr.callee.name))
        return null;
    return { fnName: callExpr.callee.name, args: callExpr.arguments };
}
function extractLiteralValue(expr) {
    if (expr.kind === 'Literal')
        return expr.value;
    return undefined;
}
/** Convert a FIL AST expression to a Flow expression string. */
function convertFILExprToFlowExpr(expr) {
    switch (expr.kind) {
        case 'Identifier':
            return expr.name;
        case 'Literal':
            return expr.raw;
        case 'BinaryExpression':
            return `${convertFILExprToFlowExpr(expr.left)} ${expr.operator} ${convertFILExprToFlowExpr(expr.right)}`;
        case 'LogicalExpression':
            return `${convertFILExprToFlowExpr(expr.left)} ${expr.operator} ${convertFILExprToFlowExpr(expr.right)}`;
        case 'MemberExpression': {
            const obj = convertFILExprToFlowExpr(expr.object);
            if (expr.computed) {
                return `${obj}[${convertFILExprToFlowExpr(expr.property)}]`;
            }
            return `${obj}.${convertFILExprToFlowExpr(expr.property)}`;
        }
        case 'CallExpression': {
            const callee = convertFILExprToFlowExpr(expr.callee);
            const args = expr.arguments.map(convertFILExprToFlowExpr).join(', ');
            return `${callee}(${args})`;
        }
        case 'AwaitExpression':
            return convertFILExprToFlowExpr(expr.argument);
        case 'UnaryExpression':
            if (expr.prefix)
                return `${expr.operator}${convertFILExprToFlowExpr(expr.argument)}`;
            return `${convertFILExprToFlowExpr(expr.argument)}${expr.operator}`;
        case 'ConditionalExpression':
            return `${convertFILExprToFlowExpr(expr.test)} ? ${convertFILExprToFlowExpr(expr.consequent)} : ${convertFILExprToFlowExpr(expr.alternate)}`;
        case 'TypeAssertion':
            return convertFILExprToFlowExpr(expr.expression);
        case 'ObjectExpression': {
            const props = expr.properties.map(p => p.shorthand ? p.key : `${p.key}: ${convertFILExprToFlowExpr(p.value)}`).join(', ');
            return `{ ${props} }`;
        }
        case 'ArrayExpression':
            return `[${expr.elements.map(convertFILExprToFlowExpr).join(', ')}]`;
        default:
            return '/* unknown */';
    }
}
/**
 * For each edge whose `sourcePort` doesn't match any source handle on the
 * source node's definition, substitute the first valid non-error source
 * handle. Edges to or from nodes without a definition (or whose definition
 * has no handleConfiguration) are left alone.
 *
 * Templated handle ids — `branch-{item.id}` style — match any concrete port
 * that has the same prefix; this handles dynamic-output node types like
 * `core.action.http` whose port count depends on configuration.
 */
function patchEdgePorts(flow) {
    const defByType = new Map();
    for (const d of flow.definitions ?? [])
        defByType.set(d.nodeType, d);
    // Patch top-level edges.
    patchEdgesAgainstNodes(flow.edges, flow.nodes, defByType);
    // Subflows (loop bodies, async-helper subflows) carry their own
    // nodes+edges; the validator validates them too, so they need the same
    // port repair.
    for (const sub of Object.values(flow.subflows ?? {})) {
        if (sub.edges && sub.nodes) {
            patchEdgesAgainstNodes(sub.edges, sub.nodes, defByType);
        }
    }
}
function patchEdgesAgainstNodes(edges, nodes, defByType) {
    const nodeById = new Map();
    for (const n of nodes)
        nodeById.set(n.id, n);
    for (const edge of edges) {
        const sourceNode = nodeById.get(edge.sourceNodeId);
        if (!sourceNode)
            continue;
        const def = defByType.get(sourceNode.type);
        if (!def?.handleConfiguration)
            continue;
        // Handle convention: `type` is the role (`source`/`target`) — that's
        // what we filter on. `handleType` is the side (`input`/`output`) and
        // `id` is the actual port name used on edges.
        const sourceHandles = [];
        for (const hg of def.handleConfiguration) {
            for (const h of hg.handles ?? []) {
                if (h.type === 'source')
                    sourceHandles.push(h.id);
            }
        }
        if (sourceHandles.length === 0)
            continue;
        if (handleMatches(edge.sourcePort, sourceHandles))
            continue;
        // Pick the first non-error source handle without a placeholder template;
        // fall back to the first overall.
        const preferred = sourceHandles.find(h => !h.includes('{') && !h.toLowerCase().includes('error'))
            ?? sourceHandles.find(h => !h.includes('{'))
            ?? sourceHandles[0];
        const oldPort = edge.sourcePort;
        edge.sourcePort = preferred;
        edge.id = edge.id.replace(`-${oldPort}-`, `-${preferred}-`);
    }
}
function handleMatches(port, handleIds) {
    for (const h of handleIds) {
        if (h === port)
            return true;
        if (h.includes('{')) {
            // Template like "branch-{item.id}" matches any "branch-XYZ" port.
            const prefix = h.split('{')[0];
            if (prefix && port.startsWith(prefix))
                return true;
        }
    }
    return false;
}
function endsWithReturn(stmts) {
    if (stmts.length === 0)
        return false;
    const last = stmts[stmts.length - 1];
    return statementReturns(last);
}
/**
 * True if a statement is guaranteed to return on every path. A direct
 * `return` qualifies. An `if/else` qualifies when BOTH arms return — in
 * that case the outer walker shouldn't emit a fall-through edge from the
 * inner decision to the outer merge (no path actually flows past the
 * inner decision).
 */
function statementReturns(stmt) {
    if (stmt.kind === 'ReturnStatement')
        return true;
    if (stmt.kind === 'BlockStatement') {
        return endsWithReturn(stmt.body);
    }
    if (stmt.kind === 'IfStatement') {
        if (!stmt.alternate)
            return false;
        const consBody = stmt.consequent.kind === 'BlockStatement'
            ? stmt.consequent.body
            : [stmt.consequent];
        const altBody = stmt.alternate.kind === 'BlockStatement'
            ? stmt.alternate.body
            : [stmt.alternate];
        return endsWithReturn(consBody) && endsWithReturn(altBody);
    }
    return false;
}
function makeTriggerNode(id, x, y) {
    return {
        id,
        type: v1_to_v2_1.NODE_TYPES.TRIGGER_MANUAL,
        typeVersion: '1.0.0',
        ui: {
            position: { x, y },
            size: { width: NODE_WIDTH, height: NODE_HEIGHT },
        },
        display: {
            label: 'Manual trigger',
            icon: 'play',
            shape: 'circle',
        },
        inputs: {},
    };
}
function makeActionNode(id, x, y) {
    return {
        id,
        type: v1_to_v2_1.NODE_TYPES.MOCK,
        typeVersion: '1.0.0',
        ui: {
            position: { x, y },
            size: { width: NODE_WIDTH, height: NODE_HEIGHT },
        },
        display: {
            label: id,
            icon: 'square-dashed',
        },
        inputs: {},
    };
}
function makeSubflowNode(id, x, y) {
    return {
        id,
        type: v1_to_v2_1.NODE_TYPES.SUBFLOW,
        typeVersion: '1.0.0',
        ui: {
            position: { x, y },
            size: { width: NODE_WIDTH, height: NODE_HEIGHT },
        },
        display: {
            label: id,
        },
        inputs: {},
    };
}
/**
 * Detect `await executeTimer(...)` and pull out the argument shape.
 * Returns null for anything else (including non-await timer calls).
 */
function extractExecuteTimerCall(expr) {
    if (expr.kind !== 'AwaitExpression')
        return null;
    const call = expr.argument;
    if (call.kind !== 'CallExpression')
        return null;
    if (call.callee.kind !== 'Identifier' || call.callee.name !== 'executeTimer')
        return null;
    if (call.arguments.length === 0)
        return null;
    const arg = call.arguments[0];
    // TimeSpan.fromMillis(<n>n) | TimeSpan.fromSeconds(<n>n) | …
    if (arg.kind === 'CallExpression' && arg.callee.kind === 'MemberExpression') {
        const obj = arg.callee.object;
        const prop = arg.callee.property;
        if (obj.kind !== 'Identifier' || prop.kind !== 'Identifier')
            return null;
        if (obj.name === 'TimeSpan' && arg.arguments.length > 0) {
            const lit = arg.arguments[0];
            if (lit.kind !== 'Literal' || typeof lit.value !== 'number')
                return null;
            const factor = prop.name === 'fromMillis' ? 1 :
                prop.name === 'fromSeconds' ? 1000 :
                    prop.name === 'fromMinutes' ? 60000 :
                        prop.name === 'fromHours' ? 3600000 :
                            prop.name === 'fromDays' ? 86400000 : null;
            if (factor === null)
                return null;
            return { kind: 'duration', durationMs: lit.value * factor };
        }
        if (obj.name === 'DateTime' && prop.name === 'fromISOString' && arg.arguments.length > 0) {
            const lit = arg.arguments[0];
            if (lit.kind !== 'Literal' || typeof lit.value !== 'string')
                return null;
            return { kind: 'date', dateValue: lit.value };
        }
    }
    return null;
}
/**
 * Detect a sync call to `__script_<id>()`. Returns the node id and the
 * captured FunctionDeclaration so the caller can recover the body.
 */
function extractScriptCall(expr, scripts) {
    // No await — scripts are sync helpers.
    if (expr.kind !== 'CallExpression')
        return null;
    if (expr.callee.kind !== 'Identifier')
        return null;
    const name = expr.callee.name;
    if (!name.startsWith(v1_to_v2_1.SCRIPT_FN_PREFIX))
        return null;
    const fn = scripts.get(name);
    if (!fn)
        return null;
    return { nodeId: name.slice(v1_to_v2_1.SCRIPT_FN_PREFIX.length), fn };
}
/**
 * Re-emit a sync helper function body as a JS string, the way it appeared
 * in the original v1 `inputs.script`. Uses fil-emitter to serialize the
 * full function and then strips the `function name(): json { ... }` wrapper.
 */
function recoverScriptBody(fn) {
    const program = { kind: 'Program', actions: [], triggers: [], functions: [fn] };
    const source = (0, v1_to_v2_1.emitProgram)(program);
    // Match the body between the first '{' and the last '}'. The emitter is
    // deterministic so this is safe for our generated wrappers.
    const open = source.indexOf('{');
    const close = source.lastIndexOf('}');
    if (open < 0 || close <= open)
        return '';
    return source.slice(open + 1, close).trim();
}
function makeDelayNode(id, timer, x, y) {
    const inputs = {};
    if (timer.kind === 'duration' && timer.durationMs !== undefined) {
        inputs.timerType = 'timeDuration';
        inputs.timerPreset = 'custom';
        inputs.timerValue = msToIso8601Duration(timer.durationMs);
    }
    else if (timer.kind === 'date' && timer.dateValue !== undefined) {
        inputs.timerType = 'timeDate';
        inputs.timerPreset = 'custom';
        inputs.timerDate = timer.dateValue;
    }
    return {
        id,
        type: v1_to_v2_1.NODE_TYPES.DELAY,
        typeVersion: '1.0.0',
        ui: {
            position: { x, y },
            size: { width: NODE_WIDTH, height: NODE_HEIGHT },
        },
        display: { label: 'Delay' },
        inputs,
        outputs: {
            output: { source: '=result.response' },
        },
    };
}
function makeScriptNode(id, body, x, y) {
    return {
        id,
        type: v1_to_v2_1.NODE_TYPES.SCRIPT,
        typeVersion: '1.0.0',
        ui: {
            position: { x, y },
            size: { width: NODE_WIDTH, height: NODE_HEIGHT },
        },
        display: { label: id },
        inputs: { script: body },
        outputs: {
            output: { source: '=result.response' },
            error: { source: '=result.Error' },
        },
    };
}
/**
 * Apply `// label: …` and `// description: …` magic-comment metadata to a
 * freshly-minted node. `script_id` is consumed at id-generation time
 * (caller's responsibility); this function only touches the display fields.
 */
function applyScriptMeta(node, meta) {
    if (!meta)
        return;
    if (meta.label)
        node.display.label = meta.label;
    if (meta.description)
        node.display.subLabel = meta.description;
}
/**
 * Post-walk pass: merge contiguous `setvar_X` const-init script nodes into a
 * single `core.action.script` that returns an object literal, with one
 * `variableUpdate` per combined var. Mutation setvars (which carry a
 * `variableUpdates` entry of their own) and user-authored `__script_<id>`
 * nodes (no `setvar_` prefix) are left alone.
 *
 * Each constituent script body must be exactly `return EXPR;`. Inside the
 * combined body the previously-declared local shadows the flow variable of
 * the same name — that's the same value the original chain would have read
 * from the flow var after each predecessor's update, so the semantics
 * survive. Downstream consumers continue to read `vars.X` via the new flow
 * global written by the per-var `variableUpdate`.
 */
function coalesceConstScriptChains(ctx, nodeVars, variableUpdates) {
    const nodeById = new Map();
    for (const node of ctx.nodes)
        nodeById.set(node.id, node);
    const outEdges = new Map();
    const inEdges = new Map();
    for (const edge of ctx.edges) {
        if (!outEdges.has(edge.sourceNodeId))
            outEdges.set(edge.sourceNodeId, []);
        outEdges.get(edge.sourceNodeId).push(edge);
        if (!inEdges.has(edge.targetNodeId))
            inEdges.set(edge.targetNodeId, []);
        inEdges.get(edge.targetNodeId).push(edge);
    }
    /** Returns `{ var, expr }` if `nodeId` is a combinable const-init script. */
    const inspect = (nodeId) => {
        const node = nodeById.get(nodeId);
        if (!node)
            return null;
        if (node.type !== v1_to_v2_1.NODE_TYPES.SCRIPT)
            return null;
        if (!node.id.startsWith('setvar_'))
            return null;
        if (variableUpdates[node.id]?.length)
            return null;
        const nv = nodeVars.find((n) => n.binding?.nodeId === node.id);
        if (!nv)
            return null;
        // Magic-comment opt-out: a `// script_id: …` / `// description: …` /
        // `// label: …` on the source statement means "treat this node as a
        // singleton" — don't absorb it into a coalesced multi-assignment block.
        if (hasAnyMagic(ctx.stmtForNode.get(node.id)))
            return null;
        const body = node.inputs?.script;
        if (typeof body !== 'string')
            return null;
        const m = /^return ([\s\S]+);$/.exec(body.trim());
        if (!m)
            return null;
        return { varName: nv.id, expr: m[1] };
    };
    const processed = new Set();
    const nodesToRemove = new Set();
    const edgesToRemove = new Set();
    for (const node of ctx.nodes) {
        if (processed.has(node.id))
            continue;
        const head = inspect(node.id);
        if (!head)
            continue;
        // Only start a chain at a node whose predecessor isn't itself combinable.
        const ins = inEdges.get(node.id) ?? [];
        if (ins.length !== 1)
            continue;
        if (inspect(ins[0].sourceNodeId))
            continue;
        const chain = [
            { id: node.id, varName: head.varName, expr: head.expr },
        ];
        processed.add(node.id);
        let cur = node.id;
        while (true) {
            const outs = outEdges.get(cur) ?? [];
            if (outs.length !== 1)
                break;
            const out = outs[0];
            if (out.sourcePort !== 'output' || out.targetPort !== 'input')
                break;
            const nextId = out.targetNodeId;
            const nextInfo = inspect(nextId);
            if (!nextInfo)
                break;
            const nextIns = inEdges.get(nextId) ?? [];
            if (nextIns.length !== 1)
                break;
            chain.push({ id: nextId, varName: nextInfo.varName, expr: nextInfo.expr });
            processed.add(nextId);
            cur = nextId;
        }
        if (chain.length < 2)
            continue;
        const headNode = nodeById.get(chain[0].id);
        const lines = [];
        for (const c of chain) {
            // Preserve any plain (non-magic) leading comments from the source
            // statement so developer-intent commentary survives into the v1
            // script body. Magic comments don't appear here — magic-bearing
            // statements opt out of coalescing entirely (see `inspect` above).
            for (const com of plainComments(ctx.stmtForNode.get(c.id))) {
                lines.push(com.raw);
            }
            lines.push(`const ${c.varName} = ${c.expr};`);
        }
        lines.push(`return { ${chain.map((c) => c.varName).join(', ')} };`);
        headNode.inputs.script = lines.join('\n');
        // Drop per-var nodeVars bindings; the combined script's `output` port now
        // holds the whole object. Replace with variableUpdates that pick out each
        // sub-key.
        for (const c of chain) {
            const idx = nodeVars.findIndex((n) => n.binding?.nodeId === c.id);
            if (idx >= 0)
                nodeVars.splice(idx, 1);
        }
        const updates = variableUpdates[chain[0].id] ?? [];
        for (const c of chain) {
            updates.push({
                variableId: c.varName,
                expression: `=js:result.response.${c.varName}`,
            });
        }
        variableUpdates[chain[0].id] = updates;
        // Remove internal chain edges (head→chain[1], chain[1]→chain[2], …) and
        // redirect the tail's outgoing edges (if any) to come from the head.
        for (let i = 0; i < chain.length - 1; i++) {
            const e = outEdges.get(chain[i].id)?.[0];
            if (e)
                edgesToRemove.add(e.id);
        }
        const tail = chain[chain.length - 1];
        for (const e of outEdges.get(tail.id) ?? []) {
            e.sourceNodeId = chain[0].id;
            e.id = `edge_${chain[0].id}-${e.sourcePort}-${e.targetNodeId}-${e.targetPort}`;
        }
        for (let i = 1; i < chain.length; i++) {
            nodesToRemove.add(chain[i].id);
        }
    }
    if (nodesToRemove.size > 0) {
        ctx.nodes = ctx.nodes.filter((n) => !nodesToRemove.has(n.id));
    }
    if (edgesToRemove.size > 0) {
        ctx.edges = ctx.edges.filter((e) => !edgesToRemove.has(e.id));
    }
}
/** Inverse of parseIso8601DurationMs in flow-to-fil. Emits PT<seconds>S. */
function msToIso8601Duration(ms) {
    if (ms === 0)
        return 'PT0S';
    const days = Math.floor(ms / 86400000);
    let rem = ms - days * 86400000;
    const hours = Math.floor(rem / 3600000);
    rem -= hours * 3600000;
    const minutes = Math.floor(rem / 60000);
    rem -= minutes * 60000;
    const seconds = rem / 1000;
    let out = 'P';
    if (days)
        out += `${days}D`;
    const hasTime = hours || minutes || seconds;
    if (hasTime)
        out += 'T';
    if (hours)
        out += `${hours}H`;
    if (minutes)
        out += `${minutes}M`;
    if (seconds)
        out += `${Number.isInteger(seconds) ? seconds : seconds}S`;
    return out;
}
function filTypeToFlow(t) {
    if (typeof t === 'string') {
        switch (t) {
            case 'string': return 'string';
            case 'f64': return 'number';
            case 'i32': return 'integer';
            case 'i64': return 'integer';
            case 'bool': return 'boolean';
            case 'json': return 'object';
            case 'void': return 'object';
            default: return 'object';
        }
    }
    if (t.kind === 'Promise')
        return filTypeToFlow(t.inner);
    if (t.kind === 'array')
        return 'array';
    return 'object';
}
/**
 * Heuristic mapping from a literal initializer to a v1 variable type.
 * Bigint literals (FIL i64) get 'integer'; other numbers split int vs number.
 */
function guessTypeFromLiteral(lit) {
    if (lit.value === null)
        return 'object';
    if (typeof lit.value === 'boolean')
        return 'boolean';
    if (typeof lit.value === 'string')
        return 'string';
    if (typeof lit.value === 'number') {
        if (lit.filType === 'i64')
            return 'integer';
        return Number.isInteger(lit.value) ? 'integer' : 'number';
    }
    return 'object';
}
/** True if any descendant of the expression is an AwaitExpression. */
function containsAwait(expr) {
    if (expr.kind === 'AwaitExpression')
        return true;
    // Limit recursion to expression-bearing children — none of the FIL
    // expression kinds we handle here have statements as children.
    switch (expr.kind) {
        case 'BinaryExpression':
        case 'LogicalExpression':
        case 'AssignmentExpression':
            return containsAwait(expr.left) ||
                containsAwait(expr.right);
        case 'UnaryExpression':
        case 'UpdateExpression':
            return containsAwait(expr.argument);
        case 'ConditionalExpression':
            return containsAwait(expr.test) || containsAwait(expr.consequent) ||
                containsAwait(expr.alternate);
        case 'CallExpression':
            if (containsAwait(expr.callee))
                return true;
            return expr.arguments.some(a => containsAwait(a));
        case 'MemberExpression':
            if (containsAwait(expr.object))
                return true;
            return expr.computed ? containsAwait(expr.property) : false;
        case 'ArrayExpression':
            return expr.elements.some(e => containsAwait(e));
        case 'ObjectExpression':
            return expr.properties.some(p => containsAwait(p.value));
        case 'TemplateLiteral':
            return expr.expressions.some(e => containsAwait(e));
        case 'TypeAssertion':
            return containsAwait(expr.expression);
        case 'SpreadElement':
            return containsAwait(expr.argument);
        case 'SequenceExpression':
            return expr.expressions.some(e => containsAwait(e));
        case 'NewExpression':
            return expr.arguments.some(a => containsAwait(a));
        default:
            return false;
    }
}
/**
 * Walk every function in the program and collect the names of locals that
 * must be materialized as flow variables. A local is materialized if:
 *
 *   1. It's mutated (`x = ...`, `x++`, `x--`, `x += ...`, etc.) — flow vars
 *      are the only way v1 carries mutable state across nodes.
 *   2. It's referenced from a position that bakes the bare identifier into
 *      a non-scope-aware v1 expression: a decision `expression`, a for-of
 *      `collection`, or a `__script_<id>` helper body (those use
 *      `convertFILExprToFlowExpr` or `emitExpression`, neither of which
 *      looks up the scope to substitute aliases).
 *
 * The pass is intentionally conservative — when in doubt, mark the name as
 * requiring a flow var. False positives only cost us a setvar node; false
 * negatives produce flows that reference unbound v1 identifiers at runtime.
 */
function collectNamesRequiringFlowVar(program) {
    const names = new Set();
    // Records every variable declaration with a non-literal initializer (both
    // `const X = …` and `let X = …`). Used after the initial walk to propagate
    // flow-var-required transitively: if X is required and its initializer
    // references Y, then Y is required too (because X's setvar body bakes Y
    // as a bare identifier).
    const varInits = new Map();
    function markIdentifiersIn(expr) {
        if (!expr)
            return;
        switch (expr.kind) {
            case 'Identifier':
                names.add(expr.name);
                return;
            case 'BinaryExpression':
            case 'LogicalExpression':
                markIdentifiersIn(expr.left);
                markIdentifiersIn(expr.right);
                return;
            case 'UnaryExpression':
                markIdentifiersIn(expr.argument);
                return;
            case 'ConditionalExpression':
                markIdentifiersIn(expr.test);
                markIdentifiersIn(expr.consequent);
                markIdentifiersIn(expr.alternate);
                return;
            case 'MemberExpression':
                markIdentifiersIn(expr.object);
                if (expr.computed)
                    markIdentifiersIn(expr.property);
                return;
            case 'CallExpression':
                markIdentifiersIn(expr.callee);
                for (const a of expr.arguments)
                    markIdentifiersIn(a);
                return;
            case 'AwaitExpression':
                markIdentifiersIn(expr.argument);
                return;
            case 'TypeAssertion':
                markIdentifiersIn(expr.expression);
                return;
            case 'ObjectExpression':
                for (const p of expr.properties)
                    markIdentifiersIn(p.value);
                return;
            case 'ArrayExpression':
                for (const e of expr.elements)
                    markIdentifiersIn(e);
                return;
            default:
                return;
        }
    }
    function collectFreeIdentifiers(expr, out) {
        if (!expr)
            return;
        switch (expr.kind) {
            case 'Identifier':
                out.add(expr.name);
                return;
            case 'BinaryExpression':
            case 'LogicalExpression':
                collectFreeIdentifiers(expr.left, out);
                collectFreeIdentifiers(expr.right, out);
                return;
            case 'UnaryExpression':
                collectFreeIdentifiers(expr.argument, out);
                return;
            case 'ConditionalExpression':
                collectFreeIdentifiers(expr.test, out);
                collectFreeIdentifiers(expr.consequent, out);
                collectFreeIdentifiers(expr.alternate, out);
                return;
            case 'MemberExpression':
                collectFreeIdentifiers(expr.object, out);
                if (expr.computed)
                    collectFreeIdentifiers(expr.property, out);
                return;
            case 'CallExpression':
                collectFreeIdentifiers(expr.callee, out);
                for (const a of expr.arguments)
                    collectFreeIdentifiers(a, out);
                return;
            case 'AwaitExpression':
                collectFreeIdentifiers(expr.argument, out);
                return;
            case 'TypeAssertion':
                collectFreeIdentifiers(expr.expression, out);
                return;
            case 'ObjectExpression':
                for (const p of expr.properties)
                    collectFreeIdentifiers(p.value, out);
                return;
            case 'ArrayExpression':
                for (const e of expr.elements)
                    collectFreeIdentifiers(e, out);
                return;
            default:
                return;
        }
    }
    function visitExprForMutations(expr) {
        if (!expr)
            return;
        switch (expr.kind) {
            case 'AssignmentExpression':
                if (expr.left.kind === 'Identifier')
                    names.add(expr.left.name);
                visitExprForMutations(expr.left);
                visitExprForMutations(expr.right);
                return;
            case 'UpdateExpression':
                if (expr.argument.kind === 'Identifier')
                    names.add(expr.argument.name);
                visitExprForMutations(expr.argument);
                return;
            case 'BinaryExpression':
            case 'LogicalExpression':
                visitExprForMutations(expr.left);
                visitExprForMutations(expr.right);
                return;
            case 'UnaryExpression':
                visitExprForMutations(expr.argument);
                return;
            case 'ConditionalExpression':
                visitExprForMutations(expr.test);
                visitExprForMutations(expr.consequent);
                visitExprForMutations(expr.alternate);
                return;
            case 'MemberExpression':
                visitExprForMutations(expr.object);
                if (expr.computed)
                    visitExprForMutations(expr.property);
                return;
            case 'CallExpression':
                visitExprForMutations(expr.callee);
                for (const arg of expr.arguments)
                    visitExprForMutations(arg);
                return;
            case 'AwaitExpression':
                visitExprForMutations(expr.argument);
                return;
            case 'ArrayExpression':
                for (const e of expr.elements)
                    visitExprForMutations(e);
                return;
            case 'ObjectExpression':
                for (const p of expr.properties)
                    visitExprForMutations(p.value);
                return;
            case 'TypeAssertion':
                visitExprForMutations(expr.expression);
                return;
            default:
                return;
        }
    }
    function visitStmt(stmt) {
        if (!stmt)
            return;
        switch (stmt.kind) {
            case 'BlockStatement':
                for (const s of stmt.body)
                    visitStmt(s);
                return;
            case 'ExpressionStatement':
                visitExprForMutations(stmt.expression);
                return;
            case 'VariableDeclaration':
                if (stmt.initializer && stmt.initializer.kind !== 'Literal') {
                    // Record for later transitive propagation.
                    varInits.set(stmt.name, stmt.initializer);
                }
                if (stmt.declarationKind === 'let' && stmt.typeAnnotation) {
                    // `let` with a type annotation may be mutated later; mark as
                    // requiring a flow var preemptively. (Mutation detection below
                    // would catch it too, but let-with-type is the typical mutable
                    // pattern so being explicit is harmless.)
                    names.add(stmt.name);
                }
                visitExprForMutations(stmt.initializer);
                return;
            case 'IfStatement':
                markIdentifiersIn(stmt.test);
                visitExprForMutations(stmt.test);
                visitStmt(stmt.consequent);
                visitStmt(stmt.alternate);
                return;
            case 'ForOfStatement':
                markIdentifiersIn(stmt.iterable);
                visitExprForMutations(stmt.iterable);
                visitStmt(stmt.body);
                return;
            case 'ForStatement':
                visitStmt(stmt.init);
                visitExprForMutations(stmt.test);
                visitExprForMutations(stmt.update);
                visitStmt(stmt.body);
                return;
            case 'WhileStatement':
                markIdentifiersIn(stmt.test);
                visitExprForMutations(stmt.test);
                visitStmt(stmt.body);
                return;
            case 'ReturnStatement':
                markIdentifiersIn(stmt.argument);
                visitExprForMutations(stmt.argument);
                return;
            case 'TryStatement':
                visitStmt(stmt.block);
                if (stmt.handler)
                    visitStmt(stmt.handler.body);
                if (stmt.finalizer)
                    visitStmt(stmt.finalizer);
                return;
            case 'SwitchStatement':
                markIdentifiersIn(stmt.discriminant);
                visitExprForMutations(stmt.discriminant);
                for (const c of stmt.cases) {
                    if (c.test) {
                        markIdentifiersIn(c.test);
                        visitExprForMutations(c.test);
                    }
                    for (const s of c.consequent)
                        visitStmt(s);
                }
                return;
            default:
                return;
        }
    }
    for (const fn of program.functions) {
        const isScriptHelper = !fn.isAsync && fn.name.startsWith(v1_to_v2_1.SCRIPT_FN_PREFIX);
        for (const stmt of fn.body.body) {
            if (isScriptHelper && stmt.kind === 'ReturnStatement') {
                markIdentifiersIn(stmt.argument);
            }
            visitStmt(stmt);
        }
    }
    // Fixpoint: if a declared variable X is required (referenced as a bare
    // identifier somewhere that bakes it into a v1 expression), then X's
    // initializer becomes a setvar body whose free identifiers are also baked
    // verbatim. Propagate the requirement transitively.
    let changed = true;
    while (changed) {
        changed = false;
        for (const [name, init] of varInits) {
            if (!names.has(name))
                continue;
            const free = new Set();
            collectFreeIdentifiers(init, free);
            for (const f of free) {
                if (!names.has(f)) {
                    names.add(f);
                    changed = true;
                }
            }
        }
    }
    return names;
}
//# sourceMappingURL=fil-to-flow.js.map