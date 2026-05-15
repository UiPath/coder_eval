#!/usr/bin/env node
"use strict";
/**
 * v1tov2 — CLI wrapper around convertV1ToV2.
 *
 * Usage:
 *   v1tov2 <input.flow> [--out-dir DIR] [--library DIR] [--write-manifest]
 *
 * Writes by default:
 *   <out-dir>/<basename>.fil           (self-contained — embeds the
 *                                       per-node data that used to live in
 *                                       the manifest sidecar)
 *   <out-dir>/bindings.json
 *
 * The `--write-manifest` flag additionally emits the legacy
 * `<basename>.manifest.flow` sidecar. Kept for incremental migration; new
 * v2 projects should not need it (`v2-to-v1` derives the equivalent
 * manifest from the FIL declarations).
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
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const index_1 = require("./index");
const library_1 = require("./library");
function fail(msg) {
    process.stderr.write(`v1tov2: ${msg}\n`);
    process.exit(1);
}
function parseArgs(argv) {
    let input;
    let outDir;
    let libraryDir;
    let writeManifest = false;
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        if (a === '--out-dir')
            outDir = argv[++i];
        else if (a === '--library')
            libraryDir = argv[++i];
        else if (a === '--write-manifest')
            writeManifest = true;
        else if (a.startsWith('--'))
            fail(`unknown flag: ${a}`);
        else if (!input)
            input = a;
        else
            fail(`unexpected positional argument: ${a}`);
    }
    if (!input)
        fail('missing input .flow file');
    return {
        input: input,
        outDir: outDir ?? path.dirname(input),
        libraryDir,
        writeManifest,
    };
}
function main() {
    const { input, outDir, libraryDir, writeManifest } = parseArgs(process.argv.slice(2));
    if (!fs.existsSync(input))
        fail(`input not found: ${input}`);
    const raw = fs.readFileSync(input, 'utf8');
    let flow;
    try {
        flow = JSON.parse(raw);
    }
    catch (e) {
        fail(`failed to parse ${input} as JSON: ${e.message}`);
    }
    const library = libraryDir ? new library_1.Library(libraryDir) : undefined;
    const result = (0, index_1.convertV1ToV2)(flow, library ? { library } : {});
    fs.mkdirSync(outDir, { recursive: true });
    const base = path.basename(input).replace(/\.flow$/, '');
    const filPath = path.join(outDir, `${base}.fil`);
    const bindingsPath = path.join(outDir, 'bindings.json');
    fs.writeFileSync(filPath, result.fil);
    fs.writeFileSync(bindingsPath, JSON.stringify(result.bindings, null, 2) + '\n');
    process.stdout.write(`Wrote ${filPath}\n`);
    process.stdout.write(`Wrote ${bindingsPath}\n`);
    if (writeManifest) {
        const manifestPath = path.join(outDir, `${base}.manifest.flow`);
        fs.writeFileSync(manifestPath, JSON.stringify(result.manifest, null, 2) + '\n');
        process.stdout.write(`Wrote ${manifestPath}\n`);
    }
    process.stdout.write(`Library entries referenced: ${result.referencedEntries.length}\n`);
    if (result.unresolvedTypes.length > 0) {
        process.stderr.write(`WARN: ${result.unresolvedTypes.length} integration node type(s) not found in the library:\n`);
        for (const t of result.unresolvedTypes) {
            process.stderr.write(`  - ${t}\n`);
        }
    }
}
main();
//# sourceMappingURL=cli.js.map