#!/usr/bin/env node
"use strict";
// CLI entry point for the FIL compiler
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
const compiler_1 = require("./compiler");
function printUsage() {
    console.error('Usage: filc [options] <input.fil>');
    console.error('');
    console.error('Options:');
    console.error('  -o <output>    Output file (default: stdout)');
    console.error('  --dump-ast     Dump the AST to stderr');
    console.error('  --dump-sm      Dump state machine info to stderr');
    console.error('  -h, --help     Show this help');
    console.error('');
    console.error('Example:');
    console.error('  filc main.fil -o main.wat');
}
function main() {
    const args = process.argv.slice(2);
    if (args.length === 0 || args.includes('-h') || args.includes('--help')) {
        printUsage();
        process.exit(args.length === 0 ? 1 : 0);
    }
    let inputFile;
    let outputFile;
    let dumpAst = false;
    let dumpSm = false;
    for (let i = 0; i < args.length; i++) {
        const arg = args[i];
        if (arg === '-o') {
            outputFile = args[++i];
        }
        else if (arg === '--dump-ast') {
            dumpAst = true;
        }
        else if (arg === '--dump-sm') {
            dumpSm = true;
        }
        else if (arg.startsWith('-')) {
            console.error(`Unknown option: ${arg}`);
            printUsage();
            process.exit(1);
        }
        else {
            inputFile = arg;
        }
    }
    if (!inputFile) {
        console.error('Error: No input file specified.');
        printUsage();
        process.exit(1);
    }
    // Read input
    let source;
    try {
        source = fs.readFileSync(inputFile, 'utf8');
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        console.error(`Error reading input file: ${msg}`);
        process.exit(1);
    }
    // Compile
    const result = (0, compiler_1.compile)(source, { dumpAst, dumpSm });
    // Print warnings
    for (const w of result.warnings) {
        console.error(`Warning: ${w}`);
    }
    // Print errors
    if (result.errors.length > 0) {
        for (const e of result.errors) {
            console.error(`Error: ${e}`);
        }
        process.exit(1);
    }
    // Write output
    if (outputFile) {
        try {
            fs.writeFileSync(outputFile, result.wat, 'utf8');
            console.error(`Written to ${outputFile}`);
        }
        catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            console.error(`Error writing output file: ${msg}`);
            process.exit(1);
        }
    }
    else {
        process.stdout.write(result.wat);
        process.stdout.write('\n');
    }
}
main();
//# sourceMappingURL=index.js.map