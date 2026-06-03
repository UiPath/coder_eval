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
import type { FlowFile } from 'v1-to-v2';
export declare function migrateInMemoryFlowTo13(flow: FlowFile): void;
//# sourceMappingURL=migrate-to-13.d.ts.map