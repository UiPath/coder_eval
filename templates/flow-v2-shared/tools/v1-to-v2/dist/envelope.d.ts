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
/** Fields that must live under essentialConfiguration when emitting enveloped. */
export declare const ESSENTIAL_KEYS: Set<string>;
/**
 * Flatten any configuration into a single dict. Accepts both flat and
 * enveloped shapes. Returns the merged keys (essential keys override flat
 * keys if both somehow exist; optional overrides essential — last-write-wins
 * preserves the optional category's defaults).
 */
export declare function flattenConfiguration(cfg: Record<string, unknown>): Record<string, unknown>;
/** Split a flattened configuration into the enveloped shape. */
export declare function envelopeConfiguration(flat: Record<string, unknown>): Record<string, unknown>;
//# sourceMappingURL=envelope.d.ts.map