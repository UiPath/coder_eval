"use strict";
/**
 * The v1 `inputs.detail.configuration` JSON-string has two shapes in the wild:
 *
 *   FLAT (older flows, including our sample-flows/new.flow):
 *     { connectorName, instanceParameters, fieldsContainer, ... }   // 28 keys
 *
 *   ENVELOPED (newer flows, e.g. the skill's Canary):
 *     {
 *       essentialConfiguration: { connectorVersion, instanceParameters, ... },
 *       optionalConfiguration:  { connectorName, fieldsContainer, ... }
 *     }
 *
 * `uip maestro flow validate` rejects flat shapes — connectors must use the
 * envelope. Both shapes carry the same field set; the envelope just groups
 * them. The split is based on a single observed Canary instance; unknown
 * fields default to `optionalConfiguration` on emit (the safer category —
 * it's the editor-cache-y bucket, not the runtime-semantic one).
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.ESSENTIAL_KEYS = void 0;
exports.flattenConfiguration = flattenConfiguration;
exports.envelopeConfiguration = envelopeConfiguration;
/** Fields that must live under essentialConfiguration when emitting enveloped. */
exports.ESSENTIAL_KEYS = new Set([
    'connectorVersion',
    'customFieldsRequestDetails',
    'executionType',
    'httpMethod',
    'instanceParameters',
    'objectName',
    'operation',
    'packageVersion',
    'path',
    'unifiedTypesCompatible',
]);
/**
 * Flatten any configuration into a single dict. Accepts both flat and
 * enveloped shapes. Returns the merged keys (essential keys override flat
 * keys if both somehow exist; optional overrides essential — last-write-wins
 * preserves the optional category's defaults).
 */
function flattenConfiguration(cfg) {
    const essential = cfg['essentialConfiguration'];
    const optional = cfg['optionalConfiguration'];
    if (!essential && !optional)
        return cfg;
    const flat = {};
    // Top-level fields first (anything outside the envelope persists).
    for (const [k, v] of Object.entries(cfg)) {
        if (k === 'essentialConfiguration' || k === 'optionalConfiguration')
            continue;
        flat[k] = v;
    }
    if (essential)
        Object.assign(flat, essential);
    if (optional)
        Object.assign(flat, optional);
    return flat;
}
/** Split a flattened configuration into the enveloped shape. */
function envelopeConfiguration(flat) {
    const essential = {};
    const optional = {};
    for (const [k, v] of Object.entries(flat)) {
        if (exports.ESSENTIAL_KEYS.has(k))
            essential[k] = v;
        else
            optional[k] = v;
    }
    return { essentialConfiguration: essential, optionalConfiguration: optional };
}
//# sourceMappingURL=envelope.js.map