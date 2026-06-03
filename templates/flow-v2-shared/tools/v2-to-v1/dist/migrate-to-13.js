"use strict";
/**
 * In-memory migration of a freshly-built FlowFile to v1.3 file format.
 *
 * `uip` 1.2.0's flow library refuses to serialize a v1.0.0-shaped workflow
 * via `uip maestro flow format` (the deploy gate the templates run), forcing
 * an external `uip maestro flow migrate` step. This module folds that
 * migration into v2-to-v1 so the converter emits v1.3 directly.
 *
 * Three structural deltas vs. v1.0.0:
 *
 *   1. Top-level `version` → `"1.3"`, `metadata` dropped (drops because the
 *      timestamps make output non-deterministic; uip accepts either).
 *   2. Every `outputs[*].source` string AND every `inputs[*]` value that's
 *      either an expression (`=…`) or sits on a non-string-typed field gets
 *      wrapped into `{ type, expression, fieldType }`:
 *        - `"=js:<expr>"` → `{ type: "jsExpression", expression: <expr>, fieldType }`
 *        - everything else → `{ type: "literal",      expression: <as-is>, fieldType }`
 *      `fieldType` comes from the node's definition (`inputDefinition.properties[k].type`
 *      / `outputDefinition.properties[k].type`); end-node output `fieldType`
 *      comes from `variables.globals[k].type`.
 *
 *   3. `node.outputs[*].source` writes the wrapped form; the per-definition
 *      `outputDefinition.*.source` schema *does not* get wrapped (that's a
 *      schema template, not a live value).
 *
 * Non-string input values (booleans, objects, arrays, numbers, null) stay as
 * native JSON; only strings get wrapped. This matches `uip maestro flow
 * migrate`'s behaviour on every sample-flows fixture.
 *
 * Per-definition `supportsErrorHandling: true` is left intact even though
 * `uip migrate` strips it — uip's format/validate accept the field, and
 * keeping it preserves round-trip fidelity with legacy fixtures that carry
 * it.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.migrateInMemoryFlowTo13 = migrateInMemoryFlowTo13;
function migrateInMemoryFlowTo13(flow) {
    flow.version = '1.3';
    delete flow.metadata;
    const schemaByType = new Map();
    for (const d of flow.definitions ?? []) {
        schemaByType.set(d.nodeType, {
            inputs: d.inputDefinition
                ?.properties,
            outputs: d.outputDefinition
                ?.properties,
        });
    }
    // End-node output `fieldType` is keyed by `variables.globals[id].type`.
    const globalTypeByName = new Map();
    for (const g of flow.variables?.globals ?? []) {
        if (g.id && g.type)
            globalTypeByName.set(g.id, g.type);
    }
    wrapNodes(flow.nodes ?? [], schemaByType, globalTypeByName);
    for (const sub of Object.values((flow.subflows ?? {}))) {
        wrapNodes(sub.nodes ?? [], schemaByType, globalTypeByName);
    }
}
function wrapNodes(nodes, schemaByType, globalTypeByName) {
    for (const n of nodes) {
        const schema = schemaByType.get(n.type);
        if (n.inputs) {
            for (const [k, v] of Object.entries(n.inputs)) {
                if (typeof v !== 'string')
                    continue;
                if (isWrappedSource(v))
                    continue;
                const declared = schema?.inputs?.[k]?.type ?? 'string';
                const expressionLike = v.startsWith('=');
                if (expressionLike || declared !== 'string') {
                    n.inputs[k] = wrap(v, declared);
                }
            }
        }
        if (n.outputs) {
            for (const [k, vobj] of Object.entries(n.outputs)) {
                if (!vobj || typeof vobj !== 'object')
                    continue;
                const src = vobj.source;
                if (typeof src !== 'string')
                    continue;
                if (isWrappedSource(src))
                    continue;
                const declared = n.type === 'core.control.end'
                    ? (globalTypeByName.get(k) ?? 'string')
                    : (schema?.outputs?.[k]?.type ?? 'string');
                vobj.source = wrap(src, declared);
            }
        }
    }
}
function isWrappedSource(v) {
    return (typeof v === 'object'
        && v !== null
        && 'type' in v
        && 'expression' in v
        && 'fieldType' in v);
}
function wrap(value, fieldType) {
    if (value.startsWith('=js:')) {
        return { type: 'jsExpression', expression: value.slice(4), fieldType };
    }
    return { type: 'literal', expression: value, fieldType };
}
//# sourceMappingURL=migrate-to-13.js.map