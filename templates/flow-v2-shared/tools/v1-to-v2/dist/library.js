"use strict";
/**
 * Loads the canonical Flow v2 connector library produced by
 * `integrations/generate_connectors.py`.
 *
 * The library lives at `<integrations>/library/` with one JSON file per
 * `(connector-key, action-id, version)` plus an `index.json` that lists every
 * entry. We read `index.json` eagerly to know what's available and lazy-load
 * individual entries on first lookup.
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
exports.Library = void 0;
exports.loadDefaultLibrary = loadDefaultLibrary;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
class Library {
    constructor(libraryDir) {
        this.indexByKey = new Map(); // key = nodeType@version
        this.cache = new Map();
        this.libraryDir = libraryDir;
        const indexPath = path.join(libraryDir, 'index.json');
        if (!fs.existsSync(indexPath)) {
            throw new Error(`Canonical library index not found at ${indexPath}. ` +
                `Did you run integrations/generate_connectors.py?`);
        }
        const raw = fs.readFileSync(indexPath, 'utf8');
        const idx = JSON.parse(raw);
        for (const e of idx.entries) {
            this.indexByKey.set(`${e.nodeType}@${e.version}`, e);
        }
    }
    has(nodeType, version) {
        return this.indexByKey.has(`${nodeType}@${version}`);
    }
    /** Returns the canonical entry, or undefined if no match. */
    lookup(nodeType, version) {
        const key = `${nodeType}@${version}`;
        if (this.cache.has(key))
            return this.cache.get(key);
        const indexEntry = this.indexByKey.get(key);
        if (!indexEntry)
            return undefined;
        const entryPath = path.join(this.libraryDir, indexEntry.path);
        const raw = fs.readFileSync(entryPath, 'utf8');
        const entry = JSON.parse(raw);
        this.cache.set(key, entry);
        return entry;
    }
    /** All known nodeType@version keys. Useful for diagnostics. */
    keys() {
        return [...this.indexByKey.keys()];
    }
}
exports.Library = Library;
/** Convenience: load the library from the default sibling location. */
function loadDefaultLibrary() {
    // ../integrations/library relative to the v1-to-v2 package root
    const here = path.resolve(__dirname, '..', '..');
    const libDir = path.join(here, 'integrations', 'library');
    return new Library(libDir);
}
//# sourceMappingURL=library.js.map