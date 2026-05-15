"use strict";
/**
 * Re-hydrate the v1 `inputs.detail.configuration` JSON-string from a v2
 * manifest node. Inverse of `distillConfiguration` in v1-to-v2.
 *
 * Composition order (later wins):
 *   1. Tier A defaults — globally constant fields the editor stamps on
 *      every connector node (additionalHeaders, language, etc.).
 *   2. Tier B values — synthesized from the canonical library entry
 *      (connectorKey, objectName, httpMethod, path, instanceParameters,
 *       connectorName, objectDisplayName).
 *   3. fieldsContainer — restored from a sidecar cache when one exists
 *      for the (connector, action, version) typeRef.
 *   4. node.configuration — fields the v1→v2 distillation kept verbatim
 *      because they don't fit any tier rule (connectorVersion, _textBlocks,
 *      unifiedTypesCompatible, etc.).
 *   5. node.configurationExtras — fields whose v1 values diverged from
 *      what the library or defaults predicted; preserved for round-trip.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.rehydrateConfiguration = rehydrateConfiguration;
const v1_to_v2_1 = require("v1-to-v2");
/** Synthesize the per-connector statics that the canonical library knows. */
function libraryDerivedFields(lib) {
    const out = {
        connectorKey: lib.connector.key,
        objectName: lib.operation.objectName,
        httpMethod: lib.operation.httpMethod,
        instanceParameters: {
            connectorKey: lib.connector.key,
            objectName: lib.operation.objectName,
            httpMethod: lib.operation.httpMethod,
            activityType: 'Curated',
            version: lib.version,
            supportsStreaming: lib.operation.supportsStreaming,
            subType: lib.operation.subType,
        },
    };
    if (lib.connector.name)
        out.connectorName = lib.connector.name;
    if (lib.operation.objectDisplayName)
        out.objectDisplayName = lib.operation.objectDisplayName;
    // Prefer the path with placeholders when available — the editor stores it
    // verbatim in `inputs.detail.configuration.path` (and on `endpoint`).
    const path = lib.operation.pathTemplate || lib.operation.path;
    if (path)
        out.path = path;
    return out;
}
function rehydrateConfiguration(opts) {
    const composed = {};
    // 1. Tier A defaults
    Object.assign(composed, v1_to_v2_1.TIER_A_DEFAULTS);
    // 2. Library-derived per-connector statics
    Object.assign(composed, libraryDerivedFields(opts.library));
    // 3. fieldsContainer from sidecar cache
    let restored = false;
    if (opts.fieldsCache) {
        const cached = opts.fieldsCache.get(opts.typeRef);
        if (cached) {
            composed.fieldsContainer = cached.blob;
            restored = true;
        }
    }
    // 4. Residual configuration the v2 file kept
    if (opts.configuration)
        Object.assign(composed, opts.configuration);
    // 5. Extras (override anything the library or defaults predicted)
    if (opts.extras)
        Object.assign(composed, opts.extras);
    // 6. Wrap in the essentialConfiguration / optionalConfiguration envelope.
    //    `uip maestro flow validate` rejects flat configurations.
    const enveloped = (0, v1_to_v2_1.envelopeConfiguration)(composed);
    return {
        config: enveloped,
        configString: '=jsonString:' + JSON.stringify(enveloped),
        fieldsContainerRestored: restored,
    };
}
//# sourceMappingURL=configuration.js.map