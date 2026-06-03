"use strict";
/**
 * Synthesize the v1 `definitions[]` array for a flow.
 *
 * Each unique typeRef referenced by the manifest's nodes corresponds to
 * one definition entry. The library generator stashes a `<entry>.v1def.json`
 * sibling next to each canonical entry — that file already has the v1
 * shape (registry `Data.Node` minus `connectorMethodInfo` and
 * `outputResponseDefinition`, plus `supportsErrorHandling`/`inputDefaults`/
 * `debug`). We just load it.
 *
 * For typeRefs without a sidecar (custom nodes, missing library entries),
 * we omit the definition. The runtime will reject the flow if it depends
 * on a definition we can't produce — that's the correct failure mode and
 * shouldn't be silently masked.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.buildDefinitions = buildDefinitions;
exports.loadActivityTypeId = loadActivityTypeId;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
function buildDefinitions(typeRefs, opts, embedded) {
    const definitions = [];
    const missing = [];
    const seenDefinitions = new Set();
    for (const typeRef of typeRefs) {
        // 1. Embedded definitions (manifest fallback) win — they came from the
        //    original v1 file, so they're guaranteed correct for round-trip.
        const embeddedDefinition = findEmbeddedDefinition(embedded, typeRef);
        if (embeddedDefinition) {
            addDefinition(definitions, seenDefinitions, embeddedDefinition);
            continue;
        }
        // 2. Otherwise, look up the canonical library v1def sidecar.
        const [nodeType, version = '1.0.0'] = typeRef.split('@');
        const def = loadV1Definition(opts.libraryDir, nodeType, version);
        if (def) {
            addDefinition(definitions, seenDefinitions, def);
        }
        else {
            missing.push(typeRef);
        }
    }
    return { definitions, missing };
}
function findEmbeddedDefinition(embedded, typeRef) {
    if (!embedded)
        return null;
    const exact = embedded[typeRef];
    if (exact)
        return exact;
    const stripped = typeRef.replace(/@(\d+\.\d+)\.\d+$/, '@$1');
    if (stripped !== typeRef && embedded[stripped]) {
        return embedded[stripped];
    }
    const expanded = typeRef.replace(/@(\d+\.\d+)$/, '@$1.0');
    if (expanded !== typeRef && embedded[expanded]) {
        return embedded[expanded];
    }
    return null;
}
function addDefinition(definitions, seenDefinitions, def) {
    const key = `${def.nodeType}@${normalizeVersion(def.version)}`;
    if (seenDefinitions.has(key))
        return;
    seenDefinitions.add(key);
    definitions.push(def);
}
function normalizeVersion(version) {
    return (version ?? '1.0.0').replace(/^(\d+\.\d+)\.\d+$/, '$1');
}
function loadV1Definition(libraryDir, nodeType, version) {
    // Mirror the layout of generate_connectors.py: the `.v1def.json` lives
    // alongside the canonical entry at <connector>/<action>@<version>.v1def.json.
    const parts = nodeType.split('.');
    if (parts.length < 4)
        return null;
    const connectorKey = parts[2];
    const actionId = parts.slice(3).join('.');
    const filename = `${actionId}@${version}.v1def.json`;
    const p = path.join(libraryDir, connectorKey, filename);
    if (!fs.existsSync(p))
        return null;
    try {
        return JSON.parse(fs.readFileSync(p, 'utf8'));
    }
    catch {
        return null;
    }
}
// Memoize per (libraryDir, typeRef) — a flow with N nodes of the same
// connector would otherwise re-read the sidecar N times. `null` records a
// looked-up-but-absent result so we don't retry on the next node.
const activityTypeIdCache = new Map();
/**
 * The stable per-(connector, action) activity id lives in the connector's
 * v1def sidecar form, at
 *   form.sections[].fields[].componentProps.connectorDetail.uiPathActivityTypeId
 *
 * `uip` >= 1.2.0 (the 2026-05 validation tightening) rejects a connector
 * activity node whose `inputs.detail.uiPathActivityTypeId` is missing —
 * "Studio Web crashes when opening this flow". The v1→v2 distillation drops
 * the field as library-determined (see v1-to-v2 `LIBRARY_DETERMINED_FIELDS`),
 * so the v2→v1 rebuild has to re-populate it from the canonical sidecar.
 *
 * Returns undefined when the sidecar is absent or predates the field (older
 * fixtures, custom nodes) — the missing-definition path already surfaces that.
 */
function loadActivityTypeId(libraryDir, nodeType, version) {
    if (!libraryDir)
        return undefined;
    const cacheKey = `${libraryDir}\n${nodeType}@${version}`;
    const cached = activityTypeIdCache.get(cacheKey);
    if (cached !== undefined)
        return cached ?? undefined;
    const def = loadV1Definition(libraryDir, nodeType, version);
    const id = def ? extractActivityTypeId(def) : undefined;
    activityTypeIdCache.set(cacheKey, id ?? null);
    return id;
}
function extractActivityTypeId(def) {
    const form = def.form;
    for (const section of form?.sections ?? []) {
        for (const field of section.fields ?? []) {
            const props = field.componentProps;
            const id = props?.connectorDetail?.uiPathActivityTypeId;
            if (typeof id === 'string' && id)
                return id;
        }
    }
    return undefined;
}
//# sourceMappingURL=definitions.js.map