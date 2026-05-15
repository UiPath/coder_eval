#!/usr/bin/env node
"use strict";
/**
 * v2tov1 — reconstruct a v1 .flow from a v2 project.
 *
 * Usage:
 *   v2tov1 <input> [--fil <name.fil>] [--manifest <name.manifest.flow>]
 *                  [--bindings <bindings.json>] [--library <dir>]
 *                  [--out <out.flow>]
 *
 * <input> can be either:
 *   - a .fil file              → preferred; manifest is synthesized from the
 *                                FIL's `flow`/`action`/`trigger` declarations
 *   - a .manifest.flow file    → legacy; the manifest is read explicitly and
 *                                the .fil is looked up alongside it
 *
 * If both `--manifest` and a `.fil` are present, the manifest is preferred
 * (acts as an override during incremental migration).
 *
 * Output: writes <out.flow> AND bindings.json (verbatim copy of the input
 * bindings) to the output directory — together they form the v1 artifact set.
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
const v1_to_v2_1 = require("v1-to-v2");
const index_1 = require("./index");
function fail(msg) {
    process.stderr.write(`v2tov1: ${msg}\n`);
    process.exit(1);
}
function parseArgs(argv) {
    let inputPath;
    let filPath;
    let manifestPath;
    let bindingsPath;
    let libraryDir;
    let outPath;
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        if (a === '--fil')
            filPath = argv[++i];
        else if (a === '--manifest')
            manifestPath = argv[++i];
        else if (a === '--bindings')
            bindingsPath = argv[++i];
        else if (a === '--library')
            libraryDir = argv[++i];
        else if (a === '--out')
            outPath = argv[++i];
        else if (a.startsWith('--'))
            fail(`unknown flag: ${a}`);
        else if (!inputPath)
            inputPath = a;
        else
            fail(`unexpected positional argument: ${a}`);
    }
    if (!inputPath)
        fail('missing input path (a .fil or .manifest.flow file)');
    return { inputPath: inputPath, filPath, manifestPath, bindingsPath, libraryDir, outPath };
}
function main() {
    const args = parseArgs(process.argv.slice(2));
    if (!fs.existsSync(args.inputPath))
        fail(`input not found: ${args.inputPath}`);
    // Classify the input. `.manifest.flow` → manifest-mode; `.fil` → FIL-mode.
    // `.flow.json` is the legacy manifest extension (kept for back-compat).
    const isManifest = args.inputPath.endsWith('.manifest.flow') ||
        args.inputPath.endsWith('.flow.json');
    const dir = path.dirname(args.inputPath);
    let basename;
    let filPath;
    let manifestPath;
    if (isManifest) {
        basename = path
            .basename(args.inputPath)
            .replace(/\.manifest\.flow$/, '')
            .replace(/\.flow\.json$/, '');
        manifestPath = args.manifestPath ?? args.inputPath;
        filPath = args.filPath ?? path.join(dir, `${basename}.fil`);
    }
    else {
        basename = path
            .basename(args.inputPath)
            .replace(/\.fil$/, '');
        filPath = args.filPath ?? args.inputPath;
        manifestPath = args.manifestPath;
        // If no --manifest, look for a sibling .manifest.flow as a fallback.
        if (!manifestPath) {
            const sibling = path.join(dir, `${basename}.manifest.flow`);
            if (fs.existsSync(sibling))
                manifestPath = sibling;
        }
    }
    if (!fs.existsSync(filPath))
        fail(`FIL source not found: ${filPath}`);
    const filSource = fs.readFileSync(filPath, 'utf8');
    let manifest = null;
    if (manifestPath && fs.existsSync(manifestPath)) {
        manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    }
    const bindingsPath = args.bindingsPath ?? path.join(dir, 'bindings.json');
    let bindings = { schemaVersion: '1', bindings: [] };
    if (fs.existsSync(bindingsPath)) {
        bindings = JSON.parse(fs.readFileSync(bindingsPath, 'utf8'));
    }
    const outPath = args.outPath ?? path.join(dir, `${basename}.flow`);
    const convertOpts = {};
    if (args.libraryDir) {
        convertOpts.library = new v1_to_v2_1.Library(args.libraryDir);
        convertOpts.libraryDir = args.libraryDir;
    }
    const result = (0, index_1.convertV2ToV1)(filSource, manifest, bindings, convertOpts);
    fs.writeFileSync(outPath, JSON.stringify(result.flow, null, 2) + '\n');
    // The v1 artifact set is <Name>.flow + bindings.json. Write bindings.json
    // alongside the output flow (verbatim copy of the input bindings).
    const outBindingsPath = path.join(path.dirname(outPath), 'bindings.json');
    fs.writeFileSync(outBindingsPath, JSON.stringify(bindings, null, 2) + '\n');
    process.stdout.write(`Wrote ${outPath}\n`);
    process.stdout.write(`Wrote ${outBindingsPath}\n`);
    process.stdout.write(`Definitions: ${result.flow.definitions?.length ?? 0}\n`);
    process.stdout.write(`Nodes: ${result.flow.nodes.length}\n`);
    process.stdout.write(`Edges: ${result.flow.edges.length}\n`);
    if (result.missingLibraryEntries.length > 0) {
        process.stderr.write(`WARN: ${result.missingLibraryEntries.length} typeRef(s) missing from library:\n`);
        for (const t of result.missingLibraryEntries)
            process.stderr.write(`  - ${t}\n`);
    }
    if (result.missingDefinitions.length > 0) {
        process.stderr.write(`WARN: ${result.missingDefinitions.length} typeRef(s) missing v1def sidecars:\n`);
        for (const t of result.missingDefinitions)
            process.stderr.write(`  - ${t}\n`);
    }
}
main();
//# sourceMappingURL=cli.js.map