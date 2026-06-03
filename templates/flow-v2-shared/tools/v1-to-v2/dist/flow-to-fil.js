"use strict";
/**
 * Converts a FlowFile to a FIL AST (Program).
 *
 * Strategy:
 * 1. Build a directed graph from Flow nodes/edges
 * 2. Walk from the start node, generating FIL statements
 * 3. Map control flow nodes to FIL constructs (if/else, for, try/catch)
 * 4. Map subflows to async helper functions
 * 5. Map variables to parameters/returns
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.SCRIPT_FN_PREFIX = exports.INLINED_NODE_TYPES = void 0;
exports.flowToFil = flowToFil;
exports.flowToFilWithOverrides = flowToFilWithOverrides;
exports.unwrapSource = unwrapSource;
exports.convertFlowExpression = convertFlowExpression;
const flow_types_1 = require("./flow-types");
const flow_graph_1 = require("./flow-graph");
/**
 * Node types that flow→FIL inlines into FIL constructs:
 *   - delay   → `await executeTimer(...)`
 *   - script  → `__script_<id>` sync helper function + call site
 *
 * `core.logic.mock` is intentionally NOT inlined — it round-trips through
 * the regular `executeNode` path. A mock node is a placeholder in flow
 * authoring (often used in tests as a stand-in for an arbitrary action),
 * and dropping it would silently change graph semantics. The "stub"
 * approach preserves it as an executeNode in FIL with empty inputs.
 *
 * Inlined node types do not carry separate entries in the v2 manifest;
 * fil→flow detects the FIL constructs to round-trip back to v1 nodes.
 */
exports.INLINED_NODE_TYPES = new Set([
    flow_types_1.NODE_TYPES.DELAY,
    flow_types_1.NODE_TYPES.SCRIPT,
]);
/** Prefix for sync helper functions that represent script nodes. */
exports.SCRIPT_FN_PREFIX = '__script_';
function flowToFil(flow) {
    const converter = new FlowToFilConverter(flow);
    return converter.convert();
}
/**
 * Converts a FlowFile to FIL AST and collects per-node FlowOverrides
 * that capture metadata not representable in FIL.
 */
function flowToFilWithOverrides(flow) {
    const converter = new FlowToFilConverter(flow, true);
    const program = converter.convert();
    const overrides = converter.buildOverrides();
    return { program, overrides };
}
/** Parser for FIL TypeScript — used to embed script bodies as sync helpers. */
const parser_1 = require("fil-compiler/dist/parser");
class FlowToFilConverter {
    constructor(flow, collectDef = false) {
        this.helperFunctions = [];
        /**
         * Sync helper functions emitted for `core.action.script` nodes, keyed by
         * the helper name (`__script_<id>`). Stored separately from
         * `helperFunctions` so we can iterate scripts independently and emit them
         * in order alongside subflow helpers.
         */
        this.scriptHelpers = new Map();
        this.nodeOverrides = {};
        /**
         * Action declarations synthesized as we see `executeNode(<id>, …)` call
         * sites. Keyed by the FIL identifier (sanitized from the v1 node id). The
         * action's body carries an `id: "<original-v1-node-id>"` field whenever
         * sanitization changed the name so the protocol layer keeps using the
         * original id verbatim.
         */
        this.actionDecls = new Map();
        this.flow = flow;
        this.collectDef = collectDef;
    }
    /**
     * Register a v1 node as a top-level `action` declaration the first time
     * it's referenced, and return the FIL identifier (a JS-safe name).
     * Subsequent calls for the same node return the same identifier.
     */
    registerAction(node) {
        const safeIdent = this.sanitizeActionIdent(node.id);
        if (this.actionDecls.has(safeIdent))
            return safeIdent;
        // Type alias if available, otherwise use the raw v1 type string as-is.
        const typeRef = this.resolveTypeRef(node.type, node.typeVersion);
        const properties = [];
        if (safeIdent !== node.id) {
            properties.push({
                kind: 'ObjectProperty',
                key: 'id',
                shorthand: false,
                value: { kind: 'Literal', value: node.id, raw: JSON.stringify(node.id) },
            });
        }
        const decl = {
            kind: 'ActionDeclaration',
            name: safeIdent,
            typeRef,
            fields: properties.length > 0
                ? { kind: 'ObjectExpression', properties }
                : undefined,
        };
        this.actionDecls.set(safeIdent, decl);
        return safeIdent;
    }
    /** Map a v1 node id to a JS-safe FIL identifier. */
    sanitizeActionIdent(id) {
        if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(id))
            return id;
        return '__act_' + id.replace(/[^A-Za-z0-9_]/g, '_');
    }
    /** v1 type string → FIL TypeRef (with optional version). */
    resolveTypeRef(typeName, version) {
        return { kind: 'TypeRef', name: typeName, version };
    }
    convert() {
        // Process subflows first (they become helper functions)
        if (this.flow.subflows) {
            for (const [subflowNodeId, subflow] of Object.entries(this.flow.subflows)) {
                this.processSubflow(subflowNodeId, subflow);
            }
        }
        // Process main flow
        const mainFn = this.convertFlowGraph('main', (0, flow_graph_1.buildGraph)(this.flow), this.flow.variables);
        // Synthesize a FlowDeclaration from the v1 flow identity. Phase 1 keeps
        // this minimal — just the required `name` and `version` fields. Phase 3
        // can add optional fields (solutionId, projectId) when present.
        const flowDecl = {
            kind: 'FlowDeclaration',
            id: this.flow.id,
            fields: {
                kind: 'ObjectExpression',
                properties: [
                    {
                        kind: 'ObjectProperty',
                        key: 'name',
                        shorthand: false,
                        value: { kind: 'Literal', value: this.flow.name, raw: JSON.stringify(this.flow.name) },
                    },
                    {
                        kind: 'ObjectProperty',
                        key: 'version',
                        shorthand: false,
                        value: { kind: 'Literal', value: this.flow.version, raw: JSON.stringify(this.flow.version) },
                    },
                ],
            },
        };
        return {
            kind: 'Program',
            flow: flowDecl,
            actions: [...this.actionDecls.values()],
            triggers: [],
            functions: [...this.scriptHelpers.values(), ...this.helperFunctions, mainFn],
        };
    }
    /**
     * Build the FlowOverrides bundle from collected per-node metadata.
     * Must be called after convert().
     */
    buildOverrides() {
        return {
            flowId: this.flow.id,
            flowName: this.flow.name,
            flowVersion: this.flow.version,
            solutionId: this.flow.solutionId,
            projectId: this.flow.projectId,
            metadata: this.flow.metadata,
            definitions: this.flow.definitions || [],
            bindings: this.flow.bindings || [],
            nodeOverrides: this.nodeOverrides,
            variableOverrides: this.flow.variables,
        };
    }
    /**
     * Record a node's metadata in the FlowOverrides bundle.
     * Inlined node types (mock/delay/script) deliberately skip collection so
     * the v2 manifest doesn't carry redundant entries for them — their
     * representation is the FIL itself.
     */
    collectNodeOverride(node) {
        if (!this.collectDef)
            return;
        if (exports.INLINED_NODE_TYPES.has(node.type))
            return;
        const override = {
            type: node.type,
            typeVersion: node.typeVersion,
        };
        if (node.display)
            override.display = node.display;
        if (node.ui)
            override.ui = node.ui;
        if (node.model)
            override.model = node.model;
        if (node.inputs) {
            // Store full inputs for round-tripping — the sidecar preserves everything
            override.inputs = node.inputs;
        }
        if (node.outputs)
            override.outputs = node.outputs;
        this.nodeOverrides[node.id] = override;
    }
    processSubflow(nodeId, subflow) {
        const graph = (0, flow_graph_1.buildSubflowGraph)(subflow);
        const fn = this.convertFlowGraph(nodeId, graph, subflow.variables);
        this.helperFunctions.push(fn);
    }
    convertFlowGraph(fnName, graph, variables) {
        const inVars = (variables?.globals || []).filter(v => v.direction === 'in');
        const outVars = (variables?.globals || []).filter(v => v.direction === 'out');
        const inoutVars = (variables?.globals || []).filter(v => v.direction === 'inout');
        // Parameters from 'in' variables
        const params = inVars.map(v => ({
            name: v.id,
            type: flowTypeToFil(v.type),
        }));
        // Return type
        let returnType;
        if (outVars.length === 0) {
            returnType = { kind: 'Promise', inner: 'void' };
        }
        else if (outVars.length === 1) {
            returnType = { kind: 'Promise', inner: flowTypeToFil(outVars[0].type) };
        }
        else {
            returnType = { kind: 'Promise', inner: 'json' };
        }
        // Build body
        const bodyStatements = [];
        // Declare 'inout' variables
        for (const v of inoutVars) {
            bodyStatements.push(makeVarDecl('let', v.id, flowTypeToFil(v.type), makeLiteral(v.defaultValue)));
        }
        // Declare output variables with defaults
        for (const v of outVars) {
            if (v.defaultValue !== undefined) {
                bodyStatements.push(makeVarDecl('let', v.id, flowTypeToFil(v.type), makeLiteral(v.defaultValue)));
            }
            else {
                bodyStatements.push(makeVarDecl('let', v.id, flowTypeToFil(v.type)));
            }
        }
        // Walk the graph
        const startNode = graph.getStartNode();
        if (startNode) {
            // Collect trigger node metadata into FlowOverrides
            this.collectNodeOverride(startNode);
            // Start from the node after the trigger
            const firstNodeId = (0, flow_types_1.isTriggerNode)(startNode.type)
                ? graph.getNextNode(startNode.id)
                : startNode.id;
            if (firstNodeId) {
                const stmts = this.walkNode(firstNodeId, graph, variables, new Set());
                bodyStatements.push(...stmts);
            }
        }
        // Return output variables (only if the body doesn't already return on all paths)
        if (outVars.length > 0 && !allPathsReturn(bodyStatements)) {
            if (outVars.length === 1) {
                bodyStatements.push(makeReturn(makeId(outVars[0].id)));
            }
            else {
                const props = outVars.map(v => ({
                    kind: 'ObjectProperty',
                    key: v.id,
                    value: makeId(v.id),
                    shorthand: true,
                }));
                bodyStatements.push(makeReturn({
                    kind: 'ObjectExpression',
                    properties: props,
                }));
            }
        }
        return {
            kind: 'FunctionDeclaration',
            name: fnName,
            isAsync: true,
            params,
            returnType,
            body: { kind: 'BlockStatement', body: bodyStatements },
        };
    }
    /**
     * Recursively walk the graph from a node, generating FIL statements.
     * `visited` prevents infinite loops.
     */
    walkNode(nodeId, graph, variables, stopAt) {
        const node = graph.getNode(nodeId);
        if (!node || stopAt.has(nodeId))
            return [];
        // Collect node metadata into FlowOverrides
        this.collectNodeOverride(node);
        const statements = [];
        switch (node.type) {
            case flow_types_1.NODE_TYPES.DECISION:
                statements.push(...this.convertDecision(node, graph, variables, stopAt));
                break;
            case flow_types_1.NODE_TYPES.SWITCH:
                statements.push(...this.convertSwitch(node, graph, variables, stopAt));
                break;
            case flow_types_1.NODE_TYPES.MERGE:
                // Merge is a passthrough — continue to next node
                statements.push(...this.continueAfter(nodeId, graph, variables, stopAt));
                break;
            case flow_types_1.NODE_TYPES.END:
                statements.push(...this.convertEnd(node));
                break;
            case flow_types_1.NODE_TYPES.TERMINATE:
                statements.push({ kind: 'ReturnStatement' });
                break;
            case flow_types_1.NODE_TYPES.SUBFLOW:
                statements.push(...this.convertSubflowCall(node, graph, variables, stopAt));
                break;
            case flow_types_1.NODE_TYPES.DELAY:
                statements.push(...this.convertDelay(node, graph, variables, stopAt));
                break;
            case flow_types_1.NODE_TYPES.SCRIPT:
                statements.push(...this.convertScript(node, graph, variables, stopAt));
                break;
            default:
                if ((0, flow_types_1.isTriggerNode)(node.type)) {
                    // Skip trigger, continue to next
                    statements.push(...this.continueAfter(nodeId, graph, variables, stopAt));
                }
                else {
                    // Regular action node → executeNode call
                    statements.push(...this.convertActionNode(node, graph, variables, stopAt));
                }
                break;
        }
        return statements;
    }
    convertActionNode(node, graph, variables, stopAt) {
        const stmts = [];
        // Build the input JSON
        const inputExpr = this.buildInputExpr(node);
        // Register the v1 node as a top-level `action` and use its FIL identifier
        // form as the first arg to `executeNode`. The action declaration carries
        // the original v1 id as a string override when sanitization renamed it.
        const actionIdent = this.registerAction(node);
        // Create: const nodeIdData = await executeNode(<actionIdent>, inputJson);
        const callExpr = {
            kind: 'AwaitExpression',
            argument: {
                kind: 'CallExpression',
                callee: makeId('executeNode'),
                arguments: [
                    makeId(actionIdent),
                    inputExpr,
                ],
            },
        };
        // Check if any downstream node or variable update references this node's output
        const isOutputUsed = this.isNodeOutputReferenced(node.id, graph, variables);
        if (isOutputUsed) {
            stmts.push(makeVarDecl('const', `${node.id}Data`, 'string', callExpr));
        }
        else {
            stmts.push({
                kind: 'ExpressionStatement',
                expression: callExpr,
            });
        }
        // Apply variable updates for this node
        const updates = variables?.variableUpdates?.[node.id];
        if (updates) {
            for (const update of updates) {
                const expr = convertFlowExpression(update.expression, node.id);
                stmts.push({
                    kind: 'ExpressionStatement',
                    expression: {
                        kind: 'AssignmentExpression',
                        operator: '=',
                        left: makeId(update.variableId),
                        right: expr,
                    },
                });
            }
        }
        // Continue to next node
        stmts.push(...this.continueAfter(node.id, graph, variables, stopAt));
        return stmts;
    }
    /**
     * Convert a `core.logic.delay` v1 node into a FIL `await executeTimer(...)`
     * statement. Maps:
     *   timerType=timeDuration, timerValue=PT15M  →  executeTimer(TimeSpan.fromMillis(900000n))
     *   timerType=timeDate,     timerDate=...    →  executeTimer(DateTime.fromISOString("..."))
     *
     * Falls back to `convertActionNode` for inputs we can't safely translate
     * (preset durations without an explicit timerValue, missing fields, etc.) —
     * those keep the executeNode shape and round-trip via the manifest.
     */
    convertDelay(node, graph, variables, stopAt) {
        const inputs = (node.inputs ?? {});
        const arg = delayArgumentExpr(inputs);
        if (!arg) {
            return this.convertActionNode(node, graph, variables, stopAt);
        }
        const callExpr = {
            kind: 'AwaitExpression',
            argument: {
                kind: 'CallExpression',
                callee: makeId('executeTimer'),
                arguments: [arg],
            },
        };
        const stmts = [
            { kind: 'ExpressionStatement', expression: callExpr },
            ...this.continueAfter(node.id, graph, variables, stopAt),
        ];
        return stmts;
    }
    /**
     * Convert a `core.action.script` v1 node into a sync helper function plus
     * its call site. The helper is named `__script_<id>` so fil→flow can
     * recognise it and recover the script body.
     *
     *   function __script_script1(): json {
     *     return test;
     *   }
     *
     *   const script1Output = __script_script1();   // when output is used
     *   __script_script1();                          // otherwise
     *
     * Falls back to `convertActionNode` if the script body fails to parse.
     */
    convertScript(node, graph, variables, stopAt) {
        const body = node.inputs?.script ?? '';
        const helperName = `${exports.SCRIPT_FN_PREFIX}${node.id}`;
        // Parse the script body inside a wrapper function. If it parses cleanly
        // we get an AST.FunctionDeclaration we can stash as a helper. If it
        // doesn't, fall back to executeNode emission.
        const helper = parseScriptHelper(helperName, body);
        if (!helper) {
            return this.convertActionNode(node, graph, variables, stopAt);
        }
        // Try natural-form dissolution first: setvar-shaped script nodes get
        // emitted as bare `x++` / `let y: T = expr;` statements instead of a
        // `__script_<id>` helper function. Returns null when no pattern
        // matches → fall through to the helper-function path.
        const natural = tryEmitNaturalForm(node, helper, variables);
        if (natural) {
            const stmts = [...natural];
            stmts.push(...this.continueAfter(node.id, graph, variables, stopAt));
            return stmts;
        }
        // Attach magic comments above the helper for any node decoration that
        // diverges from defaults. The function name already carries the v1
        // node id, so `// script_id:` isn't emitted here — only `label` and
        // `description` need a magic-comment landing pad (helper functions
        // have no declaration body to host them as fields).
        const synthesized = [];
        if (node.display?.label && node.display.label !== node.id) {
            synthesized.push(makeMagicComment('label', node.display.label));
        }
        if (node.display?.subLabel) {
            synthesized.push(makeMagicComment('description', node.display.subLabel));
        }
        if (synthesized.length > 0)
            helper.leadingComments = synthesized;
        // Stash the helper so emitter writes it alongside main(). It's a sync
        // function — `extractSubflowCall` (on the reverse path) keys off
        // `isAsync` to distinguish subflows from scripts.
        this.scriptHelpers.set(helperName, helper);
        const callExpr = {
            kind: 'CallExpression',
            callee: makeId(helperName),
            arguments: [],
        };
        const isOutputUsed = this.isNodeOutputReferenced(node.id, graph, variables);
        const stmts = [];
        if (isOutputUsed) {
            stmts.push(makeVarDecl('const', `${node.id}Data`, 'json', callExpr));
        }
        else {
            stmts.push({ kind: 'ExpressionStatement', expression: callExpr });
        }
        // Variable updates for this node, mirroring convertActionNode.
        const updates = variables?.variableUpdates?.[node.id];
        if (updates) {
            for (const update of updates) {
                const expr = convertFlowExpression(update.expression, node.id);
                stmts.push({
                    kind: 'ExpressionStatement',
                    expression: {
                        kind: 'AssignmentExpression',
                        operator: '=',
                        left: makeId(update.variableId),
                        right: expr,
                    },
                });
            }
        }
        stmts.push(...this.continueAfter(node.id, graph, variables, stopAt));
        return stmts;
    }
    convertDecision(node, graph, variables, stopAt) {
        const stmts = [];
        // Get the decision expression
        const exprStr = node.inputs?.expression || 'true';
        const testExpr = convertFlowExpression(exprStr);
        // Find merge node
        const mergeNodeId = graph.findMergeNode(node.id);
        const branchStop = new Set(stopAt);
        if (mergeNodeId)
            branchStop.add(mergeNodeId);
        // Get true and false targets
        const trueTarget = graph.getTarget(node.id, 'true');
        const falseTarget = graph.getTarget(node.id, 'false');
        const trueBranch = trueTarget
            ? this.walkNode(trueTarget, graph, variables, branchStop)
            : [];
        const falseBranch = falseTarget
            ? this.walkNode(falseTarget, graph, variables, branchStop)
            : [];
        const ifStmt = {
            kind: 'IfStatement',
            test: testExpr,
            consequent: { kind: 'BlockStatement', body: trueBranch },
            alternate: falseBranch.length > 0
                ? { kind: 'BlockStatement', body: falseBranch }
                : undefined,
        };
        stmts.push(ifStmt);
        // Continue after merge
        if (mergeNodeId) {
            stmts.push(...this.walkNode(mergeNodeId, graph, variables, stopAt));
        }
        return stmts;
    }
    convertSwitch(node, graph, variables, stopAt) {
        const stmts = [];
        const cases = node.inputs?.cases || [];
        const hasDefault = node.inputs?.hasDefault;
        const mergeNodeId = graph.findMergeNode(node.id);
        const branchStop = new Set(stopAt);
        if (mergeNodeId)
            branchStop.add(mergeNodeId);
        // Build if/else if/else chain
        let currentIf;
        let rootIf;
        for (const c of cases) {
            const target = graph.getTarget(node.id, `case-${c.id}`);
            const body = target ? this.walkNode(target, graph, variables, branchStop) : [];
            const test = convertFlowExpression(c.expression);
            const ifStmt = {
                kind: 'IfStatement',
                test,
                consequent: { kind: 'BlockStatement', body },
            };
            if (!rootIf) {
                rootIf = ifStmt;
                currentIf = ifStmt;
            }
            else if (currentIf) {
                currentIf.alternate = ifStmt;
                currentIf = ifStmt;
            }
        }
        // Default branch
        if (hasDefault && currentIf) {
            const defaultTarget = graph.getTarget(node.id, 'default');
            const defaultBody = defaultTarget
                ? this.walkNode(defaultTarget, graph, variables, branchStop)
                : [];
            if (defaultBody.length > 0) {
                currentIf.alternate = { kind: 'BlockStatement', body: defaultBody };
            }
        }
        if (rootIf)
            stmts.push(rootIf);
        // Continue after merge
        if (mergeNodeId) {
            stmts.push(...this.walkNode(mergeNodeId, graph, variables, stopAt));
        }
        return stmts;
    }
    convertEnd(node) {
        if (!node.outputs || Object.keys(node.outputs).length === 0) {
            return [{ kind: 'ReturnStatement' }];
        }
        const entries = Object.entries(node.outputs);
        if (entries.length === 1) {
            const [, val] = entries[0];
            return [makeReturn(convertFlowExpression(unwrapSource(val.source)))];
        }
        // Multiple outputs → return object
        const props = entries.map(([key, val]) => ({
            kind: 'ObjectProperty',
            key,
            value: convertFlowExpression(unwrapSource(val.source)),
            shorthand: false,
        }));
        return [makeReturn({ kind: 'ObjectExpression', properties: props })];
    }
    convertSubflowCall(node, graph, variables, stopAt) {
        const stmts = [];
        // Build arguments from subflow inputs
        const args = [];
        const subflowDef = this.flow.subflows?.[node.id];
        if (subflowDef?.variables?.globals) {
            const inVars = subflowDef.variables.globals.filter(v => v.direction === 'in');
            for (const v of inVars) {
                // Look for the value in the node's inputs
                const inputVal = node.inputs?.[v.id];
                if (inputVal !== undefined) {
                    args.push(convertFlowExpression(String(inputVal)));
                }
                else {
                    args.push(makeStringLiteral('{}'));
                }
            }
        }
        const callExpr = {
            kind: 'AwaitExpression',
            argument: {
                kind: 'CallExpression',
                callee: makeId(node.id),
                arguments: args,
            },
        };
        // Check if subflow has output variables
        const hasOutputs = subflowDef?.variables?.globals?.some(v => v.direction === 'out');
        // Check if error port is connected
        const errorTarget = graph.getTarget(node.id, 'error');
        if (errorTarget) {
            // Wrap in try/catch
            const tryBody = [];
            if (hasOutputs) {
                tryBody.push(makeVarDecl('const', `${node.id}Result`, undefined, callExpr));
            }
            else {
                tryBody.push({ kind: 'ExpressionStatement', expression: callExpr });
            }
            // Continue on success path
            const successTarget = graph.getTarget(node.id, 'output')
                || graph.getTarget(node.id, 'success');
            if (successTarget) {
                tryBody.push(...this.walkNode(successTarget, graph, variables, stopAt));
            }
            // Error path
            const catchBody = this.walkNode(errorTarget, graph, variables, stopAt);
            stmts.push({
                kind: 'TryStatement',
                block: { kind: 'BlockStatement', body: tryBody },
                handler: {
                    kind: 'CatchClause',
                    param: 'e',
                    body: { kind: 'BlockStatement', body: catchBody },
                },
            });
        }
        else {
            if (hasOutputs) {
                stmts.push(makeVarDecl('const', `${node.id}Result`, undefined, callExpr));
            }
            else {
                stmts.push({ kind: 'ExpressionStatement', expression: callExpr });
            }
            stmts.push(...this.continueAfter(node.id, graph, variables, stopAt));
        }
        return stmts;
    }
    continueAfter(nodeId, graph, variables, stopAt) {
        const next = graph.getNextNode(nodeId);
        if (next && !stopAt.has(next)) {
            return this.walkNode(next, graph, variables, stopAt);
        }
        return [];
    }
    buildInputExpr(node) {
        const inputs = node.inputs || {};
        // Connector nodes have inputs.detail with infrastructure metadata.
        // Extract only user-set field values for the FIL representation;
        // the full inputs blob is preserved in FlowOverrides.
        const detail = inputs.detail;
        if (detail && typeof detail === 'object' && detail.connector) {
            return this.buildConnectorInputExpr(detail);
        }
        // Core nodes: collect meaningful inputs (skip control-flow keys)
        const skipKeys = new Set(['expression', 'trueLabel', 'falseLabel', 'cases', 'hasDefault']);
        const filteredEntries = Object.entries(inputs).filter(([k]) => !skipKeys.has(k));
        if (filteredEntries.length === 0) {
            return makeStringLiteral('{}');
        }
        const props = filteredEntries.map(([key, value]) => ({
            kind: 'ObjectProperty',
            key,
            value: typeof value === 'string' && value.startsWith('$vars.')
                ? convertFlowExpression(value)
                : makeLiteral(value),
            shorthand: false,
        }));
        return {
            kind: 'CallExpression',
            callee: {
                kind: 'MemberExpression',
                object: makeId('JSON'),
                property: makeId('stringify'),
                computed: false,
                optional: false,
            },
            arguments: [{ kind: 'ObjectExpression', properties: props }],
        };
    }
    /**
     * Extract user-set field values from a connector node's detail object.
     * Values come from two places:
     * - detail.configuration → fieldsContainer.inputFields[].value
     * - detail.queryParameters (query-type parameter overrides)
     */
    buildConnectorInputExpr(detail) {
        const userValues = {};
        // Extract query parameter values
        if (detail.queryParameters && typeof detail.queryParameters === 'object') {
            for (const [k, v] of Object.entries(detail.queryParameters)) {
                userValues[k] = v;
            }
        }
        // Parse the configuration string to get inputFields with explicit values
        const configStr = detail.configuration || '';
        if (configStr.startsWith('=jsonString:')) {
            try {
                const config = JSON.parse(configStr.slice('=jsonString:'.length));
                const inputFields = config?.fieldsContainer?.inputFields;
                if (Array.isArray(inputFields)) {
                    for (const field of inputFields) {
                        if ('value' in field && field.value !== undefined) {
                            userValues[field.id || field.name] = field.value;
                        }
                    }
                }
            }
            catch {
                // If config parsing fails, leave userValues as-is
            }
        }
        if (Object.keys(userValues).length === 0) {
            return makeStringLiteral('{}');
        }
        const props = Object.entries(userValues).map(([key, value]) => ({
            kind: 'ObjectProperty',
            key,
            value: typeof value === 'string' && value.startsWith('$vars.')
                ? convertFlowExpression(value)
                : makeLiteral(value),
            shorthand: false,
        }));
        return {
            kind: 'CallExpression',
            callee: {
                kind: 'MemberExpression',
                object: makeId('JSON'),
                property: makeId('stringify'),
                computed: false,
                optional: false,
            },
            arguments: [{ kind: 'ObjectExpression', properties: props }],
        };
    }
    isNodeOutputReferenced(nodeId, graph, variables) {
        // Check variable updates
        if (variables?.variableUpdates?.[nodeId])
            return true;
        // Check if any downstream node references this node's output in inputs
        const allNodes = graph.getAllNodes();
        for (const n of allNodes) {
            if (!n.inputs)
                continue;
            for (const val of Object.values(n.inputs)) {
                if (typeof val === 'string' && val.includes(`$vars.${nodeId}.`)) {
                    return true;
                }
            }
        }
        return false;
    }
}
// ─── Expression conversion helpers ────────────────────────────────────────────
/**
 * Unwrap a v1.3 source object back to its v1.0-style expression string. Accepts
 * either a bare string (v1.0 shape) or `{ type, expression, fieldType }` (v1.3
 * shape); restores the `=js:` prefix when `type === 'jsExpression'`.
 */
function unwrapSource(src) {
    if (typeof src === 'string')
        return src;
    if (src && typeof src === 'object' && 'expression' in src) {
        const o = src;
        const expr = typeof o.expression === 'string' ? o.expression : '';
        return o.type === 'jsExpression' ? `=js:${expr}` : expr;
    }
    return '';
}
/**
 * Convert a Flow expression string to a FIL AST expression.
 * Flow expressions use $vars.nodeId.output syntax and =js: prefix.
 */
function convertFlowExpression(expr, contextNodeId) {
    // Strip =js: prefix
    let cleaned = expr;
    if (cleaned.startsWith('=js:')) {
        cleaned = cleaned.slice(4);
    }
    // Handle simple string literals
    if (cleaned.startsWith('"') && cleaned.endsWith('"')) {
        return makeStringLiteral(cleaned.slice(1, -1));
    }
    if (cleaned.startsWith("'") && cleaned.endsWith("'")) {
        return makeStringLiteral(cleaned.slice(1, -1));
    }
    // Handle simple number literals
    const num = Number(cleaned);
    if (!isNaN(num) && cleaned.trim() !== '') {
        return { kind: 'Literal', value: num, raw: cleaned.trim() };
    }
    // Handle boolean literals
    if (cleaned === 'true')
        return { kind: 'Literal', value: true, raw: 'true' };
    if (cleaned === 'false')
        return { kind: 'Literal', value: false, raw: 'false' };
    if (cleaned === 'null')
        return { kind: 'Literal', value: null, raw: 'null' };
    // Handle binary/logical operators (e.g., "$metadata.X == 'test'", "$vars.a > 5")
    for (const op of ['===', '!==', '==', '!=', '>=', '<=', '>', '<', '&&', '||']) {
        const parts = splitOnOperator(cleaned, op);
        if (parts) {
            const filOp = op === '==' ? '===' : op === '!=' ? '!==' : op;
            return {
                kind: filOp === '&&' || filOp === '||' ? 'LogicalExpression' : 'BinaryExpression',
                operator: filOp,
                left: convertFlowExpression(parts[0].trim(), contextNodeId),
                right: convertFlowExpression(parts[1].trim(), contextNodeId),
            };
        }
    }
    // Handle $vars references
    if (cleaned.includes('$vars.')) {
        return convertVarsExpression(cleaned, contextNodeId);
    }
    // Fallback: try to parse as a simple identifier or member expression
    return parseSimpleExpression(cleaned);
}
function convertVarsExpression(expr, contextNodeId) {
    // Replace $vars.nodeId.outputId.field with nodeIdData parsed access
    // For now, handle simple patterns
    // Check for binary operators
    for (const op of ['===', '!==', '>=', '<=', '>', '<', '&&', '||', '+', '-', '*', '/', '%']) {
        const parts = splitOnOperator(expr, op);
        if (parts) {
            return {
                kind: op === '&&' || op === '||' ? 'LogicalExpression' : 'BinaryExpression',
                operator: op,
                left: convertVarsExpression(parts[0].trim(), contextNodeId),
                right: convertVarsExpression(parts[1].trim(), contextNodeId),
            };
        }
    }
    // Handle $vars.X.Y.Z pattern
    const varsMatch = expr.match(/^\$vars\.(\w+)(?:\.(.+))?$/);
    if (varsMatch) {
        const varName = varsMatch[1];
        const rest = varsMatch[2];
        // If this references a node output (like $vars.script1.output),
        // map to the parsed data variable
        if (rest) {
            // $vars.nodeId.output.field → JSON.parse(nodeIdData).field
            const parts = rest.split('.');
            let base;
            // Check if this looks like a node output reference
            if (parts[0] === 'output' || parts[0] === 'error') {
                base = {
                    kind: 'CallExpression',
                    callee: {
                        kind: 'MemberExpression',
                        object: makeId('JSON'),
                        property: makeId('parse'),
                        computed: false,
                        optional: false,
                    },
                    arguments: [makeId(`${varName}Data`)],
                };
                // Add remaining field access
                for (let i = 1; i < parts.length; i++) {
                    base = {
                        kind: 'MemberExpression',
                        object: base,
                        property: makeId(parts[i]),
                        computed: false,
                        optional: false,
                    };
                }
            }
            else {
                // Direct field access: $vars.varName.field
                base = makeId(varName);
                for (const part of parts) {
                    base = {
                        kind: 'MemberExpression',
                        object: base,
                        property: makeId(part),
                        computed: false,
                        optional: false,
                    };
                }
            }
            return base;
        }
        return makeId(varName);
    }
    return parseSimpleExpression(expr);
}
function splitOnOperator(expr, op) {
    // Simple split that respects parentheses and quotes
    let depth = 0;
    let inString = false;
    let stringChar = '';
    for (let i = 0; i <= expr.length - op.length; i++) {
        const ch = expr[i];
        if (inString) {
            if (ch === stringChar && expr[i - 1] !== '\\')
                inString = false;
            continue;
        }
        if (ch === '"' || ch === "'") {
            inString = true;
            stringChar = ch;
            continue;
        }
        if (ch === '(' || ch === '[' || ch === '{') {
            depth++;
            continue;
        }
        if (ch === ')' || ch === ']' || ch === '}') {
            depth--;
            continue;
        }
        if (depth === 0 && expr.substring(i, i + op.length) === op) {
            const left = expr.substring(0, i);
            const right = expr.substring(i + op.length);
            if (left.trim() && right.trim()) {
                return [left, right];
            }
        }
    }
    return null;
}
function parseSimpleExpression(expr) {
    const trimmed = expr.trim();
    // Strip $ prefix from Flow variable namespaces (e.g., $metadata → metadata)
    const normalized = trimmed.startsWith('$') ? trimmed.slice(1) : trimmed;
    // Member expression: a.b.c or $metadata.FolderKey
    if (normalized.includes('.') && !normalized.includes(' ')) {
        const parts = normalized.split('.');
        let result = makeId(parts[0]);
        for (let i = 1; i < parts.length; i++) {
            result = {
                kind: 'MemberExpression',
                object: result,
                property: makeId(parts[i]),
                computed: false,
                optional: false,
            };
        }
        return result;
    }
    // Simple identifier
    if (/^[a-zA-Z_$][a-zA-Z0-9_$]*$/.test(normalized)) {
        return makeId(normalized);
    }
    // Fallback: treat as string literal
    return makeStringLiteral(trimmed);
}
// ─── AST builder helpers ──────────────────────────────────────────────────────
function makeId(name) {
    return { kind: 'Identifier', name };
}
function makeStringLiteral(value) {
    return { kind: 'Literal', value, raw: `"${value.replace(/"/g, '\\"')}"` };
}
function makeLiteral(value) {
    if (value === null || value === undefined) {
        return { kind: 'Literal', value: null, raw: 'null' };
    }
    if (typeof value === 'string')
        return makeStringLiteral(value);
    if (typeof value === 'number')
        return { kind: 'Literal', value, raw: String(value) };
    if (typeof value === 'boolean')
        return { kind: 'Literal', value, raw: String(value) };
    // Complex value → JSON.stringify
    return {
        kind: 'CallExpression',
        callee: {
            kind: 'MemberExpression',
            object: makeId('JSON'),
            property: makeId('stringify'),
            computed: false,
            optional: false,
        },
        arguments: [makeStringLiteral(JSON.stringify(value))],
    };
}
function makeVarDecl(declKind, name, type, init) {
    return {
        kind: 'VariableDeclaration',
        declarationKind: declKind,
        name,
        typeAnnotation: type,
        initializer: init,
    };
}
function makeReturn(arg) {
    return { kind: 'ReturnStatement', argument: arg };
}
function flowTypeToFil(flowType) {
    switch (flowType) {
        case 'string': return 'string';
        case 'number': return 'f64';
        case 'integer': return 'i32'; // legacy; v2-to-v1 now emits `number` for all FIL numeric types
        case 'boolean': return 'bool';
        case 'object': return 'json'; // legacy; v2-to-v1 now emits `any` for json
        case 'any': return 'json';
        case 'file': return 'file';
        case 'array': return { kind: 'array', element: 'json' };
        default: return 'json';
    }
}
/**
 * Check if all code paths in a statement list end with a return.
 * Used to avoid emitting a trailing return after exhaustive if/else returns.
 */
function allPathsReturn(stmts) {
    if (stmts.length === 0)
        return false;
    const last = stmts[stmts.length - 1];
    if (last.kind === 'ReturnStatement')
        return true;
    if (last.kind === 'IfStatement') {
        const ifStmt = last;
        if (!ifStmt.alternate)
            return false;
        const consequentReturns = ifStmt.consequent.kind === 'BlockStatement'
            ? allPathsReturn(ifStmt.consequent.body)
            : ifStmt.consequent.kind === 'ReturnStatement';
        const alternateReturns = ifStmt.alternate.kind === 'BlockStatement'
            ? allPathsReturn(ifStmt.alternate.body)
            : ifStmt.alternate.kind === 'ReturnStatement'
                ? true
                : ifStmt.alternate.kind === 'IfStatement'
                    ? allPathsReturn([ifStmt.alternate])
                    : false;
        return consequentReturns && alternateReturns;
    }
    return false;
}
// ─── Inline helpers for delay / script ───────────────────────────────────────
/**
 * Build the FIL expression for `executeTimer(...)` from a v1 delay node's
 * inputs. Returns null when the inputs don't fit one of the two supported
 * shapes; the caller falls back to executeNode emission.
 */
function delayArgumentExpr(inputs) {
    const timerType = inputs.timerType;
    if (timerType === 'timeDate') {
        const date = inputs.timerDate;
        if (!date)
            return null;
        return {
            kind: 'CallExpression',
            callee: {
                kind: 'MemberExpression',
                object: makeId('DateTime'),
                property: makeId('fromISOString'),
                computed: false,
                optional: false,
            },
            arguments: [makeStringLiteral(date)],
        };
    }
    if (timerType === 'timeDuration') {
        const value = inputs.timerValue;
        if (!value)
            return null;
        const ms = parseIso8601DurationMs(value);
        if (ms === null)
            return null;
        const literal = {
            kind: 'Literal',
            value: ms,
            raw: `${ms}n`,
            filType: 'i64',
        };
        return {
            kind: 'CallExpression',
            callee: {
                kind: 'MemberExpression',
                object: makeId('TimeSpan'),
                property: makeId('fromMillis'),
                computed: false,
                optional: false,
            },
            arguments: [literal],
        };
    }
    return null;
}
/** Parse a (subset of) ISO 8601 duration into milliseconds, or null. */
function parseIso8601DurationMs(value) {
    const m = value.match(/^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/);
    if (!m)
        return null;
    const [, d, h, mi, s] = m;
    return (Number(d || 0) * 86400000 +
        Number(h || 0) * 3600000 +
        Number(mi || 0) * 60000 +
        Number(s || 0) * 1000);
}
/**
 * Build a synthetic magic-comment AST node. Position fields are zeroed —
 * the comment didn't come from any source position; it's emitted from
 * collected v1 node metadata.
 */
function makeMagicComment(key, value) {
    return {
        raw: `// ${key}: ${value}`,
        kind: 'line',
        line: 0,
        col: 0,
        pos: 0,
        isMagic: true,
        key,
        value,
    };
}
/**
 * Structural check for the partitioner's auto-generated id shape:
 * `setvar_<lhs>_<digits>`. Done with plain string operations rather than a
 * dynamic regex — `lhs` flows in from v1 node-variable ids and we don't
 * want to interpolate it into a regex pattern.
 */
function isDefaultSetvarId(id, lhs) {
    const prefix = `setvar_${lhs}_`;
    if (!id.startsWith(prefix))
        return false;
    const suffix = id.slice(prefix.length);
    if (suffix.length === 0)
        return false;
    for (let i = 0; i < suffix.length; i++) {
        const c = suffix.charCodeAt(i);
        if (c < 0x30 || c > 0x39)
            return false; // not 0-9
    }
    return true;
}
/**
 * Attach `// script_id:` / `// label:` / `// description:` magic comments to
 * a statement when the v1 node's id/label/subLabel diverge from the defaults
 * the v2→v1 partitioner would have generated. `defaultIdLhs` is the variable
 * name the partitioner uses to build its auto-generated id pattern
 * (`setvar_<lhs>_<digit>`).
 */
function attachNonDefaultScriptMeta(stmt, node, defaultIdLhs) {
    const comments = stmt.leadingComments ? [...stmt.leadingComments] : [];
    if (!isDefaultSetvarId(node.id, defaultIdLhs)) {
        comments.push(makeMagicComment('script_id', node.id));
    }
    if (node.display?.label && node.display.label !== node.id) {
        comments.push(makeMagicComment('label', node.display.label));
    }
    if (node.display?.subLabel) {
        comments.push(makeMagicComment('description', node.display.subLabel));
    }
    if (comments.length > 0)
        stmt.leadingComments = comments;
}
/**
 * Try to emit a v1 `core.action.script` node as bare FIL statements
 * (`x++`, `let y: T = expr`, …) instead of a `__script_<id>` helper
 * function. Three recognised shapes:
 *
 *   1. Single mutation setvar: body `return <expr>;` + one variableUpdate
 *      whose expression is `=js:result.response`. Emits `<lhs> = <expr>;`
 *      with `x++` / `x--` sugar when `<expr>` is `(x + 1)` / `(x - 1)`.
 *   2. Single const-init setvar: body `return <expr>;` + one nodeVar
 *      binding to this node's output port. Emits typed
 *      `let <lhs>: <type> = <expr>;` (the `let X: T` shape preserves the
 *      flow-global convention on the v2→v1 round-trip).
 *   3. Coalesced multi-init setvar: body is a chain of `const <name> = …;`
 *      followed by `return { <name>, … };` + a variableUpdate per name
 *      whose expression is `=js:result.response.<name>`. Each declaration
 *      becomes its own `let <name>: <type> = <expr>;` statement; the head
 *      carries any non-default id/label/description magic comments.
 *
 * Returns `null` when no pattern matches — the caller falls back to the
 * existing `__script_<id>` helper-function emission path.
 */
function tryEmitNaturalForm(node, helper, variables) {
    if (node.type !== 'core.action.script')
        return null;
    const bodyStmts = helper.body.body;
    if (bodyStmts.length === 0)
        return null;
    const lastStmt = bodyStmts[bodyStmts.length - 1];
    if (lastStmt.kind !== 'ReturnStatement' || !lastStmt.argument)
        return null;
    const updates = variables?.variableUpdates?.[node.id];
    const nodeVar = variables?.nodes?.find((nv) => nv.binding?.nodeId === node.id);
    // ── Pattern 3: coalesced multi-init ───────────────────────────────────
    if (bodyStmts.length > 1) {
        const decls = [];
        for (let i = 0; i < bodyStmts.length - 1; i++) {
            if (bodyStmts[i].kind !== 'VariableDeclaration')
                return null;
            decls.push(bodyStmts[i]);
        }
        if (lastStmt.argument.kind !== 'ObjectExpression')
            return null;
        const returnObj = lastStmt.argument;
        const returnedNames = new Set();
        for (const p of returnObj.properties) {
            if (!p.shorthand)
                return null;
            returnedNames.add(p.key);
        }
        if (!updates || updates.length !== decls.length)
            return null;
        // Accept either the legacy `=js:result.response.<varName>` form
        // (each update keyed by its variableId) or the new
        // `=$vars.<nodeId>.output.<varName>` form v2-to-v1 now emits.
        for (const u of updates) {
            if (u.expression !== `=js:result.response.${u.variableId}`
                && !isVarsOutputRef(u.expression, node.id, u.variableId))
                return null;
        }
        for (const d of decls) {
            if (!returnedNames.has(d.name))
                return null;
        }
        if (nodeVar)
            return null;
        const out = [];
        for (const d of decls) {
            const globalEntry = variables?.globals?.find((g) => g.id === d.name);
            const typeAnn = globalEntry
                ? flowTypeToFil(globalEntry.type)
                : (d.typeAnnotation ?? 'json');
            const newDecl = {
                kind: 'VariableDeclaration',
                declarationKind: 'let',
                name: d.name,
                typeAnnotation: typeAnn,
                initializer: d.initializer,
            };
            // Preserve plain comments the parser attached to this decl (e.g.
            // free-form developer commentary inside the coalesced body).
            if (d.leadingComments && d.leadingComments.length > 0) {
                newDecl.leadingComments = [...d.leadingComments];
            }
            out.push(newDecl);
        }
        attachNonDefaultScriptMeta(out[0], node, decls[0].name);
        return out;
    }
    // bodyStmts.length === 1 — `return <expr>;`
    const expr = lastStmt.argument;
    // ── Pattern 1: single mutation setvar ─────────────────────────────────
    // Accept either the legacy `=js:result.response` shape or any of the
    // newer `=js:[Wrapper(]$vars.<scriptId>.output[)]` shapes. Both bind
    // the global to the script's just-returned value. For single-mutation
    // setvars the v2-to-v1 emission has no per-key suffix (the script
    // returns one value, not a `{ k: v }` object), so pass `null` as the
    // output key.
    if (updates && updates.length === 1 &&
        (updates[0].expression === '=js:result.response'
            || isVarsOutputRef(updates[0].expression, node.id, null))) {
        if (nodeVar)
            return null;
        const lhs = updates[0].variableId;
        let stmt;
        if (expr.kind === 'BinaryExpression'
            && (expr.operator === '+' || expr.operator === '-')
            && isLhsRef(expr.left, lhs)
            && expr.right.kind === 'Literal' && expr.right.value === 1) {
            // `lhs + 1` / `lhs - 1` → `lhs++` / `lhs--`
            stmt = {
                kind: 'ExpressionStatement',
                expression: {
                    kind: 'UpdateExpression',
                    operator: expr.operator === '+' ? '++' : '--',
                    argument: { kind: 'Identifier', name: lhs },
                    prefix: false,
                },
            };
        }
        else {
            stmt = {
                kind: 'ExpressionStatement',
                expression: {
                    kind: 'AssignmentExpression',
                    operator: '=',
                    left: { kind: 'Identifier', name: lhs },
                    right: expr,
                },
            };
        }
        attachNonDefaultScriptMeta(stmt, node, lhs);
        return [stmt];
    }
    // ── Pattern 2: single const-init setvar ───────────────────────────────
    // Two legal shapes:
    //   (a) Legacy: 0 updates + nodeVar binding {scriptId.output → globalName}
    //   (b) Canonical (current v2-to-v1 emission): 1 update binding the
    //       global to `=js:$vars.<scriptId>.output[ wrapped in Number/etc.]`
    //       + matching global entry, no nodeVar with this name.
    const pattern2 = (() => {
        if ((!updates || updates.length === 0) && nodeVar) {
            return { lhs: nodeVar.id, type: nodeVar.type };
        }
        if (updates && updates.length === 1 && !nodeVar) {
            const u = updates[0];
            if (!isVarsOutputRef(u.expression, node.id, null))
                return null;
            const g = variables?.globals?.find((g) => g.id === u.variableId);
            if (!g)
                return null;
            return { lhs: u.variableId, type: g.type };
        }
        return null;
    })();
    if (pattern2) {
        const typeAnn = flowTypeToFil(pattern2.type);
        const stmt = {
            kind: 'VariableDeclaration',
            declarationKind: 'let',
            name: pattern2.lhs,
            typeAnnotation: typeAnn,
            initializer: expr,
        };
        attachNonDefaultScriptMeta(stmt, node, pattern2.lhs);
        return [stmt];
    }
    return null;
}
/**
 * Strip the `$vars.` prefix from single-segment flow-global references in
 * a v1 script body before we parse it as FIL. v2-to-v1 emits `$vars.X` for
 * every flow-global reference so the v1 runtime can resolve them; in FIL
 * those same references must be bare identifiers (`X`).
 *
 * The negative lookahead `(?!\s*\.)` skips multi-segment references like
 * `$vars.<nodeId>.output` — those are node-output reads that need a
 * different rewrite (handled, if at all, downstream).
 */
function preprocessScriptBody(body) {
    return body.replace(/\$vars\.([A-Za-z_$][A-Za-z0-9_$]*)(?!\s*\.)/g, '$1');
}
/**
 * True when `varExpr` is a v2-to-v1-emitted reference to the script
 * node's own output. v2-to-v1 has emitted several shapes over time;
 * the recognizer accepts any of:
 *
 *   - `=$vars.<nodeId>.output.<varName>` (interim; no longer emitted)
 *   - `=js:$vars.<nodeId>.output` (single-mutation, no key)
 *   - `=js:$vars.<nodeId>.output.<varName>` (coalesced multi-init)
 *   - `=js:Number($vars.<nodeId>.output[.<varName>])` (with type coercion)
 *   - `=js:String($vars.<nodeId>.output[.<varName>])`
 *   - `=js:Boolean($vars.<nodeId>.output[.<varName>])`
 *
 * `outputKey` is `null` for single-mutation updates (the script returns
 * one value) and the per-key name for coalesced multi-init updates.
 */
function isVarsOutputRef(varExpr, nodeId, outputKey) {
    // Legacy interim shape (kept for back-compat with already-emitted .flow files).
    if (outputKey !== null && varExpr === `=$vars.${nodeId}.output.${outputKey}`)
        return true;
    const suffix = outputKey === null ? '' : `.${outputKey}`;
    const path = `\\$vars\\.${escapeRegex(nodeId)}\\.output${escapeRegex(suffix)}`;
    const wrapper = `(?:Number|String|Boolean)`;
    // Either bare `=js:$vars.X.output[.k]` or wrapped `=js:Wrapper($vars…)`.
    const pattern = new RegExp(`^=js:(?:${wrapper}\\()?${path}\\)?$`);
    return pattern.test(varExpr);
}
function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
/**
 * True when `expr` references the flow variable `lhs` — either as a bare
 * Identifier or as a single-segment `$vars.<lhs>` MemberExpression. The
 * latter handles cases where preprocessScriptBody didn't run (defensive
 * fallback only — preprocessing strips $vars from script bodies up front).
 */
function isLhsRef(expr, lhs) {
    if (expr.kind === 'Identifier' && expr.name === lhs)
        return true;
    if (expr.kind === 'MemberExpression'
        && !expr.computed
        && expr.object.kind === 'Identifier'
        && expr.object.name === '$vars'
        && expr.property.kind === 'Identifier'
        && expr.property.name === lhs)
        return true;
    return false;
}
/**
 * Parse a script body string as a sync FIL helper function. Returns the
 * AST.FunctionDeclaration on success, or null if the body doesn't parse —
 * caller falls back to executeNode emission then.
 */
function parseScriptHelper(name, body) {
    const stripped = preprocessScriptBody(body);
    const safeBody = stripped.trim() ? stripped : 'return null;';
    // Prepend a stub `flow` declaration — the FIL parser requires one at the
    // top of every program, even when we're only interested in extracting one
    // sync helper function out of the result.
    const wrapped = `flow _script_helper { name: "_", version: "0.0.0" };\n` +
        `function ${name}(): json {\n${safeBody}\n}\n`;
    try {
        const program = new parser_1.Parser(wrapped).parse();
        const fn = program.functions[0];
        if (!fn || fn.name !== name)
            return null;
        return fn;
    }
    catch {
        return null;
    }
}
//# sourceMappingURL=flow-to-fil.js.map