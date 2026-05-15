"use strict";
/**
 * Extract v1 bindings[] into a stand-alone bindings.json.
 *
 * The user's existing agent flow already produces bindings.json as a
 * separate file; we mirror that shape here. The v1 bindings[] entries
 * carry a fixed structure:
 *
 *   { id, name, type, resource, resourceKey, default, propertyAttribute,
 *     resourceSubType? }
 *
 * Most of those map 1:1 to v2 — we just lift them out of the .flow file
 * and into a sibling document. The manifest references them by their
 * original `id` (e.g. `bt2G1ynya`) so v1↔v2 round-trip is positional and
 * lossless.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.buildBindings = buildBindings;
function buildBindings(rawBindings) {
    return {
        schemaVersion: '1',
        bindings: (rawBindings ?? []).map((b) => b),
    };
}
//# sourceMappingURL=bindings.js.map