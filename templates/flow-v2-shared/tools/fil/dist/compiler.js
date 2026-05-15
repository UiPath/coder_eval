"use strict";
// Main compiler pipeline for the FIL compiler
// Orchestrates: Lexer -> Parser -> TypeChecker -> AwaitLifter -> WatEmitter
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
exports.compile = compile;
const parser_1 = require("./parser");
const typechecker_1 = require("./typechecker");
const await_lifter_1 = require("./await_lifter");
const wat_emitter_1 = require("./wat_emitter");
const AST = __importStar(require("./ast"));
const STAGE_LABELS = {
    'parse': 'Parse',
    'typecheck': 'Type',
    'await-lift': 'Await lift',
    'emit': 'WAT emit',
};
/** Pull "line N:M" or "line N" out of an error message; return undefined if absent. */
function extractPosition(msg) {
    const m = msg.match(/at line (\d+):(\d+)/);
    if (m) {
        return {
            line: parseInt(m[1], 10),
            col: parseInt(m[2], 10),
            // Strip the "at line N:M" suffix; leave the rest of the message intact.
            cleaned: msg.replace(/\s*at line \d+:\d+/, '').trim(),
        };
    }
    const m2 = msg.match(/line (\d+)/);
    if (m2)
        return { line: parseInt(m2[1], 10), cleaned: msg };
    return { cleaned: msg };
}
function pushError(errors, errorDetails, stage, rawMsg) {
    const { line, col, cleaned } = extractPosition(rawMsg);
    errorDetails.push({ stage, message: cleaned, line, col });
    errors.push(`${STAGE_LABELS[stage]} error: ${rawMsg}`);
}
function compile(source, options = {}) {
    const errors = [];
    const errorDetails = [];
    const warnings = [];
    // ── 1. Parse ─────────────────────────────────────────────────────────────
    let program;
    try {
        const parser = new parser_1.Parser(source);
        program = parser.parse();
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        pushError(errors, errorDetails, 'parse', msg);
        return { wat: '', errors, errorDetails, warnings };
    }
    if (options.dumpAst) {
        process.stderr.write('=== AST ===\n');
        process.stderr.write(JSON.stringify(program, null, 2) + '\n');
    }
    // ── 2. Type check ─────────────────────────────────────────────────────────
    const typeChecker = new typechecker_1.TypeChecker();
    try {
        typeChecker.check(program);
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        pushError(errors, errorDetails, 'typecheck', msg);
        return { wat: '', errors, errorDetails, warnings };
    }
    const functions = typeChecker.getFunctions();
    // ── 3. Await lifting / state machine transform ───────────────────────────
    const lifter = new await_lifter_1.AwaitLifter();
    let stateMachines;
    try {
        stateMachines = lifter.transform(program, functions);
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        pushError(errors, errorDetails, 'await-lift', msg);
        return { wat: '', errors, errorDetails, warnings };
    }
    if (options.dumpSm) {
        process.stderr.write('=== State Machines ===\n');
        for (const [name, info] of stateMachines) {
            process.stderr.write(`  ${name}: ${info.numStates} states, frame size ${info.frameLayout.frameSize}\n`);
            for (const slot of info.frameLayout.slots) {
                process.stderr.write(`    [${slot.offset}] ${slot.name}: ${AST.typeToString(slot.type)} ${slot.isParam ? '(param)' : ''}\n`);
            }
        }
    }
    // ── 4. WAT emission ────────────────────────────────────────────────────────
    let wat;
    try {
        const emitter = new wat_emitter_1.WatEmitter(functions, stateMachines, lifter);
        wat = emitter.emit(program);
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        pushError(errors, errorDetails, 'emit', msg);
        return { wat: '', errors, errorDetails, warnings };
    }
    return { wat, errors, errorDetails, warnings };
}
//# sourceMappingURL=compiler.js.map