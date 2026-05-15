"use strict";
/**
 * Distill the v1 `inputs.detail.configuration` JSON-string blob into a
 * minimal per-instance form for Flow v2.
 *
 * Empirical analysis of curated-connector configurations (see findings in
 * the design discussion) shows that across all observed instances, every
 * field falls into one of three buckets:
 *
 *   Tier A — globally constant defaults (15 fields). Strip; restore from
 *            the DEFAULTS table on v2→v1.
 *   Tier B — per-connector statics (some library-derivable, some only
 *            available with a connection-id-enriched registry call). Strip
 *            when the value matches what the canonical library already
 *            knows; keep otherwise.
 *   Tier C — `fieldsContainer` (16–51 KB editor-cache mirror of the
 *            connector's input/output schemas). Strip when its content
 *            matches the per-connector sidecar cache; keep verbatim
 *            otherwise. The cache is populated opportunistically: the
 *            FIRST flow we convert that uses a given (connector, action)
 *            captures the blob; subsequent conversions strip it.
 *
 * Anything that doesn't match a tier rule is preserved in an `extras`
 * object on the manifest entry so we never lose data on unfamiliar
 * connectors or future editor versions.
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
exports.FileSystemFieldsCache = exports.TIER_A_DEFAULTS = void 0;
exports.distillConfiguration = distillConfiguration;
exports.loadDefaultFieldsCache = loadDefaultFieldsCache;
const crypto = __importStar(require("crypto"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const envelope_1 = require("./envelope");
// ─── Tier A: globally constant defaults ────────────────────────────────────
//
// Every observed v1 integration node carried these exact values regardless
// of connector. Stripping them costs ~3% per node, but documenting the
// defaults is what makes lossless v2→v1 reconstruction possible.
exports.TIER_A_DEFAULTS = {
    additionalHeaders: null,
    baseObject: null,
    cachedBrowserItems: [],
    cachedLookupValues: [],
    cachedPropertyMappings: [],
    customFieldsRequestDetails: null,
    eventMode: null,
    eventOperation: null,
    executionType: null,
    hasBreakingChanges: false,
    isAutopilotInvalidConfig: false,
    jobArguments: null,
    language: 'en-US',
    maxPageSize: 1000,
    objectActions: [],
    operation: 'create',
    packageVersion: '1.0.0',
    primaryKeys: [],
};
// ─── Tier B: per-connector statics derivable from the canonical library ────
/**
 * For each Tier B field, return the value the library says it should have,
 * or `undefined` if the library doesn't know. Stripped on v2 emission only
 * when the v1 value matches.
 */
function libraryValue(field, lib) {
    switch (field) {
        case 'connectorKey': return lib.connector.key;
        case 'connectorName': return lib.connector.name || undefined;
        case 'objectName': return lib.operation.objectName;
        case 'httpMethod': return lib.operation.httpMethod;
        case 'objectDisplayName': return lib.operation.objectDisplayName || undefined;
        case 'path': return lib.operation.pathTemplate || lib.operation.path || undefined;
        case 'instanceParameters': {
            // The editor synthesises this dict from the canonical operation/connector
            // fields. Same shape across every instance of a given action.
            return {
                connectorKey: lib.connector.key,
                objectName: lib.operation.objectName,
                httpMethod: lib.operation.httpMethod,
                activityType: 'Curated',
                version: lib.version,
                supportsStreaming: lib.operation.supportsStreaming,
                subType: lib.operation.subType,
            };
        }
        default:
            return undefined;
    }
}
const TIER_B_FIELDS = [
    'connectorKey', 'connectorName', 'objectName', 'httpMethod',
    'objectDisplayName', 'path', 'instanceParameters',
];
// ─── Tier C: fieldsContainer sidecar cache ─────────────────────────────────
const CACHE_DIRNAME = 'cache'; // sibling of integrations/library/
const CACHE_SUFFIX = '.fieldsContainer.json';
class FileSystemFieldsCache {
    constructor(cacheRoot) {
        this.memo = new Map();
        this.cacheDir = cacheRoot;
    }
    pathFor(typeRef) {
        // typeRef is e.g. "uipath.connector.uipath-microsoft-github.create-issue@1.0.0"
        const [nodeType, version] = typeRef.split('@');
        const parts = nodeType.split('.');
        if (parts.length < 4)
            throw new Error(`unexpected typeRef: ${typeRef}`);
        const connectorKey = parts[2];
        const action = parts.slice(3).join('.');
        return path.join(this.cacheDir, connectorKey, `${action}@${version}${CACHE_SUFFIX}`);
    }
    get(typeRef) {
        if (this.memo.has(typeRef))
            return this.memo.get(typeRef) ?? undefined;
        const p = this.pathFor(typeRef);
        if (!fs.existsSync(p)) {
            this.memo.set(typeRef, null);
            return undefined;
        }
        const entry = JSON.parse(fs.readFileSync(p, 'utf8'));
        this.memo.set(typeRef, entry);
        return entry;
    }
    put(typeRef, blob, source) {
        if (this.get(typeRef))
            return false;
        const canonical = JSON.stringify(blob);
        const entry = {
            hash: hash(canonical),
            size: canonical.length,
            capturedFrom: source,
            capturedAt: new Date().toISOString(),
            blob,
        };
        const p = this.pathFor(typeRef);
        fs.mkdirSync(path.dirname(p), { recursive: true });
        fs.writeFileSync(p, JSON.stringify(entry, null, 2) + '\n');
        this.memo.set(typeRef, entry);
        return true;
    }
}
exports.FileSystemFieldsCache = FileSystemFieldsCache;
function hash(text) {
    return crypto.createHash('sha256').update(text).digest('hex');
}
function distillConfiguration(config, opts) {
    // Normalize: enveloped configurations get flattened before tiering.
    config = (0, envelope_1.flattenConfiguration)(config);
    const kept = {};
    const extras = {};
    const stats = { tierA: 0, tierB: 0, tierC: 0, extras: 0 };
    let fieldsContainerStripped = false;
    for (const [k, v] of Object.entries(config)) {
        // Tier A — strip when value matches the documented default.
        if (k in exports.TIER_A_DEFAULTS) {
            if (deepEqual(v, exports.TIER_A_DEFAULTS[k])) {
                stats.tierA++;
            }
            else {
                // Unexpected non-default value — preserve in extras so it round-trips.
                extras[k] = v;
                stats.extras++;
            }
            continue;
        }
        // Tier B — strip when value matches the library's known value.
        if (TIER_B_FIELDS.includes(k) && opts.library) {
            const expected = libraryValue(k, opts.library);
            if (expected !== undefined && deepEqual(v, expected)) {
                stats.tierB++;
                continue;
            }
            // Library didn't have a value or values diverged — keep.
            if (expected === undefined) {
                kept[k] = v;
            }
            else {
                // Divergent — into extras (and future v1→v2 lossless reconstruction).
                extras[k] = v;
                stats.extras++;
            }
            continue;
        }
        // Tier C — fieldsContainer.
        if (k === 'fieldsContainer') {
            const stripped = handleFieldsContainer(v, opts);
            if (stripped) {
                fieldsContainerStripped = true;
                stats.tierC++;
            }
            else {
                kept[k] = v;
            }
            continue;
        }
        // Unknown field — into kept (per-instance varying) for now. We can
        // promote to extras if we later learn it's deterministic.
        kept[k] = v;
    }
    return { kept, extras, fieldsContainerStripped, stats };
}
function handleFieldsContainer(blob, opts) {
    if (!opts.cache)
        return false;
    const cached = opts.cache.get(opts.typeRef);
    if (!cached) {
        // First time seeing this connector — capture and strip on subsequent runs.
        if (opts.source)
            opts.cache.put(opts.typeRef, blob, opts.source);
        return false; // keep verbatim THIS time so the v2 file is self-contained
        // even if the cache disappears
    }
    // Compare by canonical-JSON hash — exact match means safe to strip.
    const incoming = JSON.stringify(blob);
    if (hash(incoming) === cached.hash)
        return true;
    return false;
}
function deepEqual(a, b) {
    if (a === b)
        return true;
    if (a === null || b === null)
        return false;
    if (typeof a !== typeof b)
        return false;
    if (typeof a !== 'object')
        return false;
    const aIsArr = Array.isArray(a);
    const bIsArr = Array.isArray(b);
    if (aIsArr !== bIsArr)
        return false;
    if (aIsArr) {
        const aa = a;
        const bb = b;
        if (aa.length !== bb.length)
            return false;
        for (let i = 0; i < aa.length; i++) {
            if (!deepEqual(aa[i], bb[i]))
                return false;
        }
        return true;
    }
    const ao = a;
    const bo = b;
    const ak = Object.keys(ao);
    const bk = Object.keys(bo);
    if (ak.length !== bk.length)
        return false;
    for (const k of ak) {
        if (!(k in bo))
            return false;
        if (!deepEqual(ao[k], bo[k]))
            return false;
    }
    return true;
}
/** Convenience: load the default sidecar cache from integrations/cache/. */
function loadDefaultFieldsCache() {
    const here = path.resolve(__dirname, '..', '..');
    const cacheDir = path.join(here, 'integrations', CACHE_DIRNAME);
    return new FileSystemFieldsCache(cacheDir);
}
//# sourceMappingURL=configuration.js.map