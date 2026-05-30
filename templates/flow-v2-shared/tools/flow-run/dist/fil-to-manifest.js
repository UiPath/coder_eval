"use strict";
/**
 * Derive a flow-run `FlowManifest` from a parsed FIL `Program`'s top-level
 * `flow` / `action` / `trigger` declarations.
 *
 * Mirrors v2-to-v1's `filProgramToManifest`: walks `program.actions` +
 * `program.triggers` and rebuilds the manifest shape the dispatcher and
 * preflight resolver consume.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.filProgramToManifest = filProgramToManifest;
/**
 * Short typeRef aliases that map to canonical v1 node types. Anything not in
 * this map is treated as already-qualified (the typeRef is used verbatim).
 * Kept in sync with v2-to-v1/src/fil-to-manifest.ts.
 */
const TYPE_ALIASES = {
    // Triggers
    start: 'core.trigger.manual',
    scheduled: 'core.trigger.scheduled',
    webhook: 'core.trigger.webhook',
    // Actions
    http: 'core.action.http',
    script: 'core.action.script',
    transform: 'core.action.transform',
    // Logic
    decision: 'core.logic.decision',
    merge: 'core.logic.merge',
    loop: 'core.logic.loop',
    switch: 'core.logic.switch',
    delay: 'core.logic.delay',
    mock: 'core.logic.mock',
    // Control
    end: 'core.control.end',
    terminate: 'core.logic.terminate',
    // Subflow
    subflow: 'core.subflow',
};
function filProgramToManifest(program) {
    if (!program.flow) {
        throw new Error('FIL has no `flow` declaration. Phase 4 requires `flow <id> { name: "…", version: "…" };` at the top of the file.');
    }
    const flowFields = readObjectFields(program.flow.fields);
    const flowName = stringField(flowFields, 'name') ?? program.flow.id;
    const flowVersion = stringField(flowFields, 'version') ?? '1.0.0';
    const manifest = {
        schemaVersion: '1',
        flowId: program.flow.id,
        flowName,
        flowVersion,
        nodes: {},
    };
    const solutionId = stringField(flowFields, 'solutionId');
    if (solutionId)
        manifest.solutionId = solutionId;
    const projectId = stringField(flowFields, 'projectId');
    if (projectId)
        manifest.projectId = projectId;
    for (const trigger of program.triggers) {
        const id = resolveNodeId(trigger);
        manifest.nodes[id] = buildManifestNode(trigger.typeRef, trigger.fields);
    }
    for (const action of program.actions) {
        const id = resolveNodeId(action);
        manifest.nodes[id] = buildManifestNode(action.typeRef, action.fields);
    }
    return manifest;
}
function resolveNodeId(decl) {
    if (!decl.fields)
        return decl.name;
    const id = stringField(readObjectFields(decl.fields), 'id');
    return id ?? decl.name;
}
function buildManifestNode(typeRef, body) {
    const canonicalType = TYPE_ALIASES[typeRef.name] ?? typeRef.name;
    const version = typeRef.version ?? '1.0.0';
    const node = { type: `${canonicalType}@${version}` };
    if (!body)
        return node;
    const fields = readObjectFields(body);
    const label = stringField(fields, 'label');
    if (label !== undefined)
        node.label = label;
    const binding = stringField(fields, 'binding');
    if (binding !== undefined)
        node.binding = binding;
    const folderBinding = stringField(fields, 'folderBinding');
    if (folderBinding !== undefined)
        node.folderBinding = folderBinding;
    const rawInputs = objectField(fields, 'rawInputs');
    if (rawInputs)
        node.rawInputs = rawInputs;
    const inputs = objectField(fields, 'inputs');
    if (inputs)
        node.inputs = inputs;
    const configuration = objectField(fields, 'configuration');
    if (configuration)
        node.configuration = configuration;
    const outputs = objectField(fields, 'outputs');
    if (outputs)
        node.outputs = outputs;
    const fixture = anyLiteralField(fields, 'fixture');
    if (fixture !== undefined)
        node.fixture = fixture;
    // Process-resource actions (Agent, API Workflow, etc.) carry resource
    // metadata at the top level of the body. Cast through ManifestNode's
    // optional shapes.
    const resource = objectField(fields, 'resource');
    if (resource)
        node.resource = resource;
    const resourceBindings = objectField(fields, 'resourceBindings');
    if (resourceBindings) {
        node.resourceBindings = resourceBindings;
    }
    return node;
}
function readObjectFields(obj) {
    const out = new Map();
    for (const p of obj.properties)
        out.set(p.key, p.value);
    return out;
}
function stringField(fields, name) {
    const e = fields.get(name);
    if (e && e.kind === 'Literal' && typeof e.value === 'string')
        return e.value;
    return undefined;
}
function objectField(fields, name) {
    const e = fields.get(name);
    if (!e || e.kind !== 'ObjectExpression')
        return undefined;
    return evalObjectLiteral(e);
}
/**
 * Constant-fold any JSON-literal field (object, array, string, number,
 * bool, null). Returns `undefined` for non-literal expressions. Used for
 * fields like `fixture` whose value can be any JSON shape.
 */
function anyLiteralField(fields, name) {
    const e = fields.get(name);
    if (!e)
        return undefined;
    return evalLiteralExpr(e);
}
function evalObjectLiteral(obj) {
    const out = {};
    for (const p of obj.properties) {
        const v = evalLiteralExpr(p.value);
        if (v !== undefined)
            out[p.key] = v;
    }
    return out;
}
function evalLiteralExpr(expr) {
    if (expr.kind === 'Literal')
        return expr.value;
    if (expr.kind === 'ObjectExpression')
        return evalObjectLiteral(expr);
    if (expr.kind === 'ArrayExpression') {
        const out = [];
        for (const e of expr.elements) {
            const v = evalLiteralExpr(e);
            if (v !== undefined)
                out.push(v);
        }
        return out;
    }
    if (expr.kind === 'TemplateLiteral' && expr.expressions.length === 0) {
        return expr.quasis.join('');
    }
    return undefined;
}
//# sourceMappingURL=fil-to-manifest.js.map