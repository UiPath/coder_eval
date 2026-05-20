"use strict";
/**
 * Phase A wire-up — populate v1 `inputs.detail.configuration` from a FIL
 * program's `executeNode("nodeId", {…})` argument expressions.
 *
 * Intent: an agent-authored FIL flow can write
 *
 *   await executeNode("sendEmail", { to: vars.user.email, subject: "Hi" });
 *
 * without ever touching the v2 manifest. The expansion in `manifest.ts`
 * still rebuilds the connector's scaffolding (Tier A defaults, library-
 * derived statics, fieldsContainer cache, …); this module folds the FIL
 * argument's per-instance field values into that scaffolding using the
 * tier-A v1 expression classifier.
 *
 * Walking strategy: we re-scan the FIL AST tracking a Scope (FIL name → v1
 * path) at each call site. The scope is built the same way `filToFlow`
 * builds it for the round-trip's nodeVar bindings — function params bind
 * to `vars.<name>`, every variable declaration adds another `vars.<name>`,
 * for-of iterators add `iterator.<name>` in their child scope. We do this
 * walk independently rather than threading scope out of `filToFlow` so
 * each executeNode call gets the *exact* scope visible at its location.
 *
 * Storage:
 *   - If the connector configuration's `fieldsContainer.inputFields` has
 *     an entry whose `id`/`name` matches the FIL key, set its `value`.
 *   - Otherwise stash the value in `optionalConfiguration[<key>]` (a
 *     generic, validate-tolerant catch-all).
 *
 * Values that don't classify (await, arbitrary calls, etc.) are recorded
 * on the `skipped` list so the caller can surface them as conversion
 * errors. We never throw from this pass — partial wire-up is still a
 * better v1 file than nothing.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.applyFilInputExpressions = applyFilInputExpressions;
const expression_1 = require("./expression");
/**
 * Walk every `await executeNode("nodeId", <argExpr>)` in `main()` (and any
 * subflow function), classify the input expression's properties, and merge
 * the resolved v1 expressions into the corresponding flow node's
 * `inputs.detail.configuration`.
 *
 * Mutates `flow` in place. Returns a diagnostic record.
 */
function applyFilInputExpressions(flow, program) {
    const result = { applied: 0, skipped: [], unknownNodeIds: [] };
    const nodeById = new Map();
    for (const n of flow.nodes)
        nodeById.set(n.id, n);
    // Subflow bodies (loop bodies, async-helper subflows) carry the actual
    // connector nodes when iteration / helpers wrap them. The FIL
    // `executeNode("nodeId", …)` reference matches whether the node ended up
    // top-level or inside a subflow.
    for (const sub of Object.values(flow.subflows ?? {})) {
        for (const n of sub.nodes ?? [])
            nodeById.set(n.id, n);
    }
    // Action identifier → effective protocol id. Lets us match
    // `executeNode(<ident>, …)` calls to flow node ids.
    const actionIds = new Map();
    for (const a of program.actions) {
        const idProp = a.fields?.properties.find((p) => p.key === 'id');
        const id = idProp && idProp.value.kind === 'Literal' && typeof idProp.value.value === 'string'
            ? idProp.value.value
            : a.name;
        actionIds.set(a.name, id);
    }
    for (const fn of program.functions) {
        const scope = seedFromParams(fn);
        walkBody(fn.body.body, scope, nodeById, actionIds, result);
    }
    return result;
}
function seedFromParams(fn) {
    const scope = new Map();
    for (const p of fn.params)
        scope.set(p.name, `$vars.${p.name}`);
    return scope;
}
function walkBody(stmts, scope, nodeById, actionIds, result) {
    for (const stmt of stmts)
        walkStmt(stmt, scope, nodeById, actionIds, result);
}
function walkStmt(stmt, scope, nodeById, actionIds, result) {
    switch (stmt.kind) {
        case 'VariableDeclaration': {
            // Every declared local resolves to vars.<name> in v1 (filToFlow
            // makes it a flow global — see Phase B/D).
            scope.set(stmt.name, `$vars.${stmt.name}`);
            if (stmt.initializer) {
                tryApplyToCall(stmt.initializer, scope, nodeById, actionIds, result);
            }
            return;
        }
        case 'ExpressionStatement':
            tryApplyToCall(stmt.expression, scope, nodeById, actionIds, result);
            return;
        case 'BlockStatement':
            walkBody(stmt.body, scope, nodeById, actionIds, result);
            return;
        case 'IfStatement': {
            const cons = stmt.consequent.kind === 'BlockStatement'
                ? stmt.consequent.body
                : [stmt.consequent];
            walkBody(cons, scope, nodeById, actionIds, result);
            if (stmt.alternate) {
                const alt = stmt.alternate.kind === 'BlockStatement'
                    ? stmt.alternate.body
                    : [stmt.alternate];
                walkBody(alt, scope, nodeById, actionIds, result);
            }
            return;
        }
        case 'ForOfStatement': {
            // Iterator var binds to `iterator.<name>` in the body's scope.
            const inner = new Map(scope);
            inner.set(stmt.item, `iterator.${stmt.item}`);
            const body = stmt.body.kind === 'BlockStatement'
                ? stmt.body.body
                : [stmt.body];
            walkBody(body, inner, nodeById, actionIds, result);
            return;
        }
        case 'ForStatement': {
            const inner = new Map(scope);
            if (stmt.init && stmt.init.kind === 'VariableDeclaration') {
                inner.set(stmt.init.name, `$vars.${stmt.init.name}`);
            }
            const body = stmt.body.kind === 'BlockStatement'
                ? stmt.body.body
                : [stmt.body];
            walkBody(body, inner, nodeById, actionIds, result);
            return;
        }
        case 'ForInStatement': {
            const inner = new Map(scope);
            inner.set(stmt.key, `iterator.${stmt.key}`);
            const body = stmt.body.kind === 'BlockStatement'
                ? stmt.body.body
                : [stmt.body];
            walkBody(body, inner, nodeById, actionIds, result);
            return;
        }
        case 'WhileStatement': {
            const body = stmt.body.kind === 'BlockStatement'
                ? stmt.body.body
                : [stmt.body];
            walkBody(body, scope, nodeById, actionIds, result);
            return;
        }
        case 'TryStatement': {
            walkBody(stmt.block.body, scope, nodeById, actionIds, result);
            if (stmt.handler) {
                const inner = new Map(scope);
                if (stmt.handler.param) {
                    inner.set(stmt.handler.param, `$vars.${stmt.handler.param}`);
                }
                walkBody(stmt.handler.body.body, inner, nodeById, actionIds, result);
            }
            if (stmt.finalizer)
                walkBody(stmt.finalizer.body, scope, nodeById, actionIds, result);
            return;
        }
        case 'SwitchStatement': {
            for (const c of stmt.cases) {
                walkBody(c.consequent, scope, nodeById, actionIds, result);
            }
            return;
        }
        default:
            return;
    }
}
function tryApplyToCall(expr, scope, nodeById, actionIds, result) {
    let call;
    if (expr.kind === 'AwaitExpression' && expr.argument.kind === 'CallExpression') {
        call = expr.argument;
    }
    else if (expr.kind === 'CallExpression') {
        call = expr;
    }
    if (!call)
        return;
    if (call.callee.kind !== 'Identifier' || call.callee.name !== 'executeNode')
        return;
    const nameArg = call.arguments[0];
    let nodeId;
    if (nameArg && nameArg.kind === 'Identifier' && actionIds.has(nameArg.name)) {
        nodeId = actionIds.get(nameArg.name);
    }
    else if (nameArg && nameArg.kind === 'Literal' && typeof nameArg.value === 'string') {
        nodeId = nameArg.value;
    }
    else {
        return;
    }
    const argExpr = call.arguments[1];
    if (!argExpr)
        return;
    // The flow-to-fil emitter currently wraps the input object in JSON.stringify
    // for connector nodes, but agent-authored FIL may pass a bare object literal.
    // Accept both. A literal `"{}"` (empty inputs) maps to no work.
    const inner = unwrapJsonStringify(argExpr) ?? argExpr;
    if (inner.kind !== 'ObjectExpression')
        return;
    if (inner.properties.length === 0)
        return;
    const node = nodeById.get(nodeId);
    if (!node) {
        result.unknownNodeIds.push(nodeId);
        return;
    }
    applyObjectToNode(node, inner, scope, result);
}
function unwrapJsonStringify(expr) {
    if (expr.kind !== 'CallExpression')
        return null;
    const callee = expr.callee;
    if (callee.kind !== 'MemberExpression' || callee.computed)
        return null;
    if (callee.object.kind !== 'Identifier' || callee.object.name !== 'JSON')
        return null;
    if (callee.property.kind !== 'Identifier' || callee.property.name !== 'stringify')
        return null;
    return expr.arguments[0] ?? null;
}
function applyObjectToNode(node, obj, scope, result) {
    const inputs = (node.inputs ?? (node.inputs = {}));
    // HTTP nodes (and other flat-input non-integration nodes) write FIL fields
    // straight onto `node.inputs`, overriding any manifest rawInputs values for
    // the same keys. Headers/queryParams are passed through verbatim (the v1
    // expression for an ObjectExpression is `={"k":"v",...}` which the v1
    // runtime interprets correctly).
    const isHttp = typeof node.type === 'string' &&
        (node.type === 'core.action.http' || node.type === 'core.action.http.v2');
    if (isHttp) {
        for (const prop of obj.properties) {
            const r = (0, expression_1.tryEmitV1Expression)(prop.value, scope);
            if (!r) {
                result.skipped.push({
                    nodeId: node.id, field: prop.key,
                    reason: 'expression not in v1-expressible subset',
                });
                continue;
            }
            inputs[prop.key] = r.expression;
            result.applied++;
        }
        return;
    }
    // Only integration nodes carry inputs.detail.configuration.
    const detail = inputs.detail;
    if (!detail || typeof detail !== 'object')
        return;
    // Configuration may be a `=jsonString:{...}` string (rehydrated form)
    // OR an object literal (intermediate forms in tests). Handle both and
    // re-serialize in the same shape we found.
    let cfg;
    let cfgWasString = false;
    if (typeof detail.configuration === 'string') {
        cfgWasString = true;
        const raw = detail.configuration;
        const prefix = '=jsonString:';
        const body = raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
        try {
            cfg = JSON.parse(body);
        }
        catch {
            cfg = {};
        }
    }
    else if (typeof detail.configuration === 'object' && detail.configuration !== null) {
        cfg = detail.configuration;
    }
    else {
        cfg = {};
    }
    const inputFields = findInputFieldsArray(cfg);
    for (const prop of obj.properties) {
        const r = (0, expression_1.tryEmitV1Expression)(prop.value, scope);
        if (!r) {
            result.skipped.push({
                nodeId: node.id,
                field: prop.key,
                reason: 'expression not in v1-expressible subset',
            });
            continue;
        }
        // Prefer updating an existing fieldsContainer entry. The cache entry
        // ships `id` AND/OR `name`; match on whichever is present.
        const existing = inputFields?.find((f) => f.id === prop.key || f.name === prop.key);
        if (existing) {
            existing.value = r.expression;
        }
        else {
            const opt = (cfg.optionalConfiguration ?? (cfg.optionalConfiguration = {}));
            opt[prop.key] = r.expression;
        }
        result.applied++;
    }
    if (cfgWasString) {
        detail.configuration = '=jsonString:' + JSON.stringify(cfg);
    }
    else {
        detail.configuration = cfg;
    }
}
/**
 * Find the inputFields array in a configuration. Handles both the flat shape
 * (top-level `fieldsContainer`) and the enveloped shape (essential / optional
 * each potentially holding one).
 */
function findInputFieldsArray(cfg) {
    const candidates = [
        cfg.fieldsContainer,
        cfg.essentialConfiguration?.fieldsContainer,
        cfg.optionalConfiguration?.fieldsContainer,
    ];
    for (const fc of candidates) {
        if (fc && typeof fc === 'object' && Array.isArray(fc.inputFields)) {
            return fc.inputFields;
        }
    }
    return null;
}
//# sourceMappingURL=apply-input-expressions.js.map