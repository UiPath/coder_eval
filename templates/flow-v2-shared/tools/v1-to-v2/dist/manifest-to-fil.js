"use strict";
/**
 * Phase 3 of the manifest-removal work: bake `ManifestFile` per-node data
 * into the FIL's `action` and `trigger` declaration bodies, so the FIL is
 * self-contained and the `<Name>.manifest.flow` sidecar can be dropped.
 *
 * Called from `convertV1ToV2` after `flowToFil` and `buildManifest` have run.
 * The flow-to-fil emitter already produced minimal action declarations (one
 * per executeNode-driven v1 node, body holding just an `id` override when
 * the identifier was sanitised). This pass:
 *
 *   - Adds `label`, `binding`, `folderBinding`, `rawInputs`, `inputs`,
 *     `configuration`, `configurationExtras`, `outputs`, `resource`,
 *     `resourceBindings`, and `fixture` to each action's body from the
 *     matching manifest entry.
 *   - Synthesises a `TriggerDeclaration` for every manifest entry whose
 *     type is `core.trigger.*`.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.populateProgramFromManifest = populateProgramFromManifest;
function populateProgramFromManifest(program, manifest, 
/**
 * Ids of nodes that live at the top level of the v1 flow (i.e. in
 * `flow.nodes`, not inside `flow.subflows[*].nodes`). Trigger entries
 * outside this set are subflow-internal and don't become top-level
 * `trigger` declarations on the program. Pass an empty/undefined set to
 * accept every trigger entry (useful for non-flow callers that build a
 * manifest standalone).
 */
topLevelNodeIds) {
    // Index actions by effective protocol id (the `id` override if present,
    // otherwise the FIL identifier). Matches how v2-to-v1 keys actions.
    const actionsById = new Map();
    for (const a of program.actions)
        actionsById.set(actionProtocolId(a), a);
    const seenTriggerIds = new Set(program.triggers.map((t) => t.name));
    for (const [nodeId, manifestNode] of Object.entries(manifest.nodes)) {
        const { typeName, version } = splitTypeRef(manifestNode.type);
        if (typeName.startsWith('core.trigger.')) {
            if (seenTriggerIds.has(nodeId))
                continue;
            if (topLevelNodeIds && !topLevelNodeIds.has(nodeId))
                continue;
            const decl = {
                kind: 'TriggerDeclaration',
                name: nodeId,
                typeRef: { kind: 'TypeRef', name: typeName, version },
                fields: buildFieldsBody(manifestNode),
            };
            program.triggers.push(decl);
            continue;
        }
        const action = actionsById.get(nodeId);
        if (!action)
            continue;
        // Merge: action.fields may already carry an `id` override; preserve it.
        const existing = action.fields?.properties ?? [];
        const existingKeys = new Set(existing.map((p) => p.key));
        const extra = buildFieldsBody(manifestNode);
        if (extra) {
            const merged = [...existing];
            for (const p of extra.properties) {
                if (!existingKeys.has(p.key))
                    merged.push(p);
            }
            action.fields = { kind: 'ObjectExpression', properties: merged };
        }
    }
}
function actionProtocolId(action) {
    if (!action.fields)
        return action.name;
    const idProp = action.fields.properties.find((p) => p.key === 'id');
    if (idProp && idProp.value.kind === 'Literal' && typeof idProp.value.value === 'string') {
        return idProp.value.value;
    }
    return action.name;
}
function splitTypeRef(typeRef) {
    const at = typeRef.lastIndexOf('@');
    if (at < 0)
        return { typeName: typeRef, version: '1.0.0' };
    return { typeName: typeRef.slice(0, at), version: typeRef.slice(at + 1) };
}
/**
 * Build the action/trigger body from a manifest node's per-instance fields.
 * Returns undefined when the node carries no manifest-side data worth
 * embedding (the action body stays bare in the emitted FIL).
 */
function buildFieldsBody(node) {
    const props = [];
    addStringProp(props, 'label', node.label);
    addStringProp(props, 'binding', node.binding);
    addStringProp(props, 'folderBinding', node.folderBinding);
    addObjectProp(props, 'rawInputs', node.rawInputs);
    addObjectProp(props, 'inputs', node.inputs);
    addObjectProp(props, 'configuration', node.configuration);
    addObjectProp(props, 'configurationExtras', node.configurationExtras);
    addObjectProp(props, 'outputs', node.outputs);
    addObjectProp(props, 'resource', node.resource);
    addObjectProp(props, 'resourceBindings', node.resourceBindings);
    addAnyProp(props, 'fixture', node.fixture);
    if (props.length === 0)
        return undefined;
    return { kind: 'ObjectExpression', properties: props };
}
function addStringProp(props, key, value) {
    if (value === undefined || value === '')
        return;
    props.push({
        kind: 'ObjectProperty',
        key,
        shorthand: false,
        value: { kind: 'Literal', value, raw: JSON.stringify(value) },
    });
}
function addObjectProp(props, key, value) {
    if (!value || Object.keys(value).length === 0)
        return;
    props.push({
        kind: 'ObjectProperty',
        key,
        shorthand: false,
        value: valueToAst(value),
    });
}
/**
 * Emit a body field whose value can be any JSON shape (object, array,
 * string, number, bool, null) — used for `fixture`, where the mock's
 * configured output can be anything the dispatcher needs to return.
 */
function addAnyProp(props, key, value) {
    if (value === undefined)
        return;
    props.push({
        kind: 'ObjectProperty',
        key,
        shorthand: false,
        value: valueToAst(value),
    });
}
/**
 * Convert a plain JS value (parsed from JSON in the v1 NodeInstance) into a
 * FIL `Expression`. The FIL emitter renders this back to source so the
 * round-trip preserves the value's shape verbatim.
 */
function valueToAst(value) {
    if (value === null) {
        return { kind: 'Literal', value: null, raw: 'null' };
    }
    if (typeof value === 'string') {
        return { kind: 'Literal', value, raw: JSON.stringify(value) };
    }
    if (typeof value === 'number') {
        return { kind: 'Literal', value, raw: String(value) };
    }
    if (typeof value === 'boolean') {
        return { kind: 'Literal', value, raw: String(value) };
    }
    if (Array.isArray(value)) {
        return {
            kind: 'ArrayExpression',
            elements: value.map(valueToAst),
        };
    }
    if (typeof value === 'object') {
        const props = [];
        for (const [k, v] of Object.entries(value)) {
            props.push({
                kind: 'ObjectProperty',
                key: k,
                shorthand: false,
                value: valueToAst(v),
            });
        }
        return { kind: 'ObjectExpression', properties: props };
    }
    // Bigints, functions, symbols, undefined — none of these belong in a
    // manifest node, but fall back to null rather than crashing the emitter.
    return { kind: 'Literal', value: null, raw: 'null' };
}
//# sourceMappingURL=manifest-to-fil.js.map