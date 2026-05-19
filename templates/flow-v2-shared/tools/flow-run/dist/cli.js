#!/usr/bin/env node
"use strict";
// flow-run: execute a Flow v2 project (FIL + bindings) locally.
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
const loader_1 = require("./loader");
const runner_1 = require("./runner");
const connections_1 = require("./connections");
const compile_errors_1 = require("./compile-errors");
const wasi_host_1 = require("./wasi-host");
function printUsage() {
    process.stderr.write(`Usage: flow-run <project-dir> [options]\n` +
        `\n` +
        `Run a Flow v2 (FIL + bindings) locally against supported dispatchers.\n` +
        `The FIL's top-level \`flow\`/\`action\`/\`trigger\` declarations carry the per-node\n` +
        `data (typeRef, bindings, rawInputs, agent metadata) — no manifest sidecar.\n` +
        `\n` +
        `Options:\n` +
        `  --fil <path>          FIL source (default: <dir>/*.fil)\n` +
        `  --bindings <path>     bindings.json (default: <dir>/bindings.json)\n` +
        `  --library <dir>       Connector library (default: <repo>/integrations/library)\n` +
        `  --history <path>      History file (default: <dir>/.flow-run/history.yaml)\n` +
        `  --decisions <path>    Decisions JSON (default: <dir>/.flow-run/decisions.json)\n` +
        `  --input <json>        Trigger input payload (default: {})\n` +
        `  --resume              Continue from existing history file\n` +
        `  --dry-run             Print dispatches; do not execute live calls (required for process-resource nodes)\n` +
        `  --skip-connection-check\n` +
        `                        Don't call 'uip is connections list' (offline mode)\n` +
        `  --max-steps <n>       Cap on replay iterations (default: 1000)\n` +
        `  --tenant <name>       Forwarded to 'uip is ...'\n` +
        `  --real-time           For executeTimer decisions, actually sleep (default: record deadline immediately)\n` +
        `  -v, --verbose         Log each step\n` +
        `  -h, --help            Show this help\n`);
}
function parseArgs(argv) {
    const args = {
        resume: false,
        dryRun: false,
        skipConnectionCheck: false,
        verbose: false,
        maxSteps: 1000,
        realTime: false,
    };
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        switch (a) {
            case '-h':
            case '--help':
                printUsage();
                process.exit(0);
                break;
            case '--fil':
                args.fil = argv[++i];
                break;
            case '--bindings':
                args.bindings = argv[++i];
                break;
            case '--library':
                args.library = argv[++i];
                break;
            case '--history':
                args.history = argv[++i];
                break;
            case '--decisions':
                args.decisions = argv[++i];
                break;
            case '--input':
                args.input = argv[++i];
                break;
            case '--resume':
                args.resume = true;
                break;
            case '--dry-run':
                args.dryRun = true;
                break;
            case '--skip-connection-check':
                args.skipConnectionCheck = true;
                break;
            case '--max-steps':
                args.maxSteps = parseInt(argv[++i], 10);
                break;
            case '--tenant':
                args.tenant = argv[++i];
                break;
            case '--real-time':
                args.realTime = true;
                break;
            case '-v':
            case '--verbose':
                args.verbose = true;
                break;
            default:
                if (a.startsWith('-')) {
                    process.stderr.write(`unknown option: ${a}\n`);
                    process.exit(2);
                }
                if (args.projectDir) {
                    process.stderr.write(`unexpected positional: ${a}\n`);
                    process.exit(2);
                }
                args.projectDir = a;
        }
    }
    if (!args.projectDir) {
        printUsage();
        process.exit(2);
    }
    return args;
}
function findLibraryDir(projectDir) {
    let cur = path.resolve(projectDir);
    while (true) {
        const candidate = path.join(cur, 'integrations', 'library');
        if (fs.existsSync(candidate))
            return candidate;
        const parent = path.dirname(cur);
        if (parent === cur) {
            throw new Error(`could not find integrations/library/ above ${projectDir}. Pass --library explicitly.`);
        }
        cur = parent;
    }
}
async function main() {
    const args = parseArgs(process.argv.slice(2));
    const projectDir = path.resolve(args.projectDir);
    const project = (0, loader_1.loadProject)(projectDir, {
        filPath: args.fil,
        bindingsPath: args.bindings,
    });
    const libraryDir = args.library ? path.resolve(args.library) : findLibraryDir(projectDir);
    // Fetch connections (unless skipped). We do this before pre-flight so
    // binding verification has the data.
    let connections = null;
    if (!args.skipConnectionCheck && !args.dryRun) {
        try {
            connections = (0, connections_1.fetchConnections)({ tenant: args.tenant });
            if (args.verbose) {
                process.stderr.write(`flow-run: fetched ${connections.length} connection(s) from your tenant\n`);
            }
        }
        catch (e) {
            const err = e;
            process.stderr.write(`flow-run: warning — could not fetch connections list: ${err.message}\n`);
            if (err.stderr)
                process.stderr.write(`  ${err.stderr.split('\n').slice(0, 3).join('\n  ')}\n`);
            process.stderr.write(`  (proceeding without connection verification; pass --skip-connection-check to silence this)\n`);
        }
    }
    const report = (0, loader_1.preflight)(project, libraryDir, connections);
    process.stderr.write(`flow-run: ${project.flowName} (${project.flowId})\n`);
    process.stderr.write(`  fil:       ${project.filPath}\n`);
    if (project.bindingsPath)
        process.stderr.write(`  bindings:  ${project.bindingsPath}\n`);
    process.stderr.write(`  library:   ${libraryDir}\n`);
    process.stderr.write(`  nodes resolved: ${report.nodes.length}\n`);
    for (const n of report.nodes) {
        let detail;
        switch (n.kind) {
            case 'connector':
                detail = `${n.verb} ${n.connectorKey}/${n.objectName} (connection ${n.connectionId.slice(0, 8)}...)`;
                break;
            case 'http':
                detail = 'HTTP';
                break;
            case 'mock':
                detail = `mock (${n.fixture === undefined ? '{}' : 'fixture'})`;
                break;
            case 'script':
                detail = 'script';
                break;
            case 'agent':
                detail = `agent ${n.name || n.resourceKey || ''}`.trim();
                break;
            case 'api-workflow':
                detail = `api-workflow ${n.name || n.resourceKey || ''}`.trim();
                break;
            case 'rpa-workflow':
                detail = `rpa-workflow ${n.name || n.resourceKey || ''}`.trim();
                break;
            case 'inline-agent':
                detail = `inline-agent ${n.source}`;
                break;
            case 'hitl':
                detail = `hitl ${n.taskType}`;
                break;
            case 'summarize':
                detail = 'summarize';
                break;
        }
        process.stderr.write(`    - ${n.nodeId.padEnd(24)} ${detail}\n`);
    }
    for (const w of report.warnings)
        process.stderr.write(`  warn:  ${w}\n`);
    for (const e of report.errors)
        process.stderr.write(`  error: ${e}\n`);
    if (report.errors.length > 0) {
        process.stderr.write(`pre-flight failed; aborting.\n`);
        process.exit(1);
    }
    const opts = {
        projectDir,
        filPath: project.filPath,
        bindingsPath: project.bindingsPath,
        historyPath: args.history ?? path.join(projectDir, '.flow-run', 'history.yaml'),
        decisionsPath: args.decisions ?? path.join(projectDir, '.flow-run', 'decisions.json'),
        inputJson: args.input ?? '{}',
        resume: args.resume,
        dryRun: args.dryRun,
        verbose: args.verbose,
        maxSteps: args.maxSteps,
        tenant: args.tenant,
        libraryDir,
        verifyConnections: !args.skipConnectionCheck,
        realTime: args.realTime,
    };
    let runReport;
    try {
        runReport = await (0, runner_1.runFlow)(project, report.nodes, opts);
    }
    catch (e) {
        if (e instanceof wasi_host_1.FilCompileError) {
            process.stderr.write('\n');
            process.stderr.write((0, compile_errors_1.formatCompileErrors)(e.errors, project.filSource, project.filPath));
            process.stderr.write('\n');
            process.exit(1);
        }
        if (e instanceof wasi_host_1.WasmTimeoutError) {
            process.stderr.write(`\n✗ ${e.message}\n`);
            process.exit(1);
        }
        throw e;
    }
    if (runReport.success) {
        process.stderr.write(`✓ flowCompleted (${runReport.history.events.length} decisions)\n`);
        process.stderr.write(`  history:   ${opts.historyPath}\n`);
        process.stderr.write(`  decisions: ${opts.decisionsPath}\n`);
        process.exit(0);
    }
    else {
        process.stderr.write(`✗ run failed: ${runReport.error}\n`);
        process.stderr.write(`  history:   ${opts.historyPath} (${runReport.history.events.length} decisions persisted)\n`);
        process.stderr.write(`  decisions: ${opts.decisionsPath}\n`);
        process.exit(1);
    }
}
main().catch(e => {
    process.stderr.write(`fatal: ${e instanceof Error ? e.stack ?? e.message : String(e)}\n`);
    process.exit(1);
});
//# sourceMappingURL=cli.js.map