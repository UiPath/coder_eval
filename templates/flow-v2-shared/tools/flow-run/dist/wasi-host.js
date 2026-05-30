"use strict";
// WASI host that runs a FIL-compiled WASM module with a fixed stdin buffer
// and captures stdout. The actual WASM execution happens in a worker thread
// so we can hard-cap it at WASM_TIMEOUT_MS — synchronous WASM in the main
// thread can't be preempted otherwise.
//
// Per a single FIL replay, runtime should be CPU-bound (only fd_read/fd_write
// I/O via WASI; no network, no file system). 5s is generous for any sane
// flow; anything that exceeds it is almost certainly a runaway loop.
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
exports.WasmTimeoutError = exports.FilCompileError = exports.WASM_TIMEOUT_MS = void 0;
exports.compileFil = compileFil;
exports.runWasm = runWasm;
exports.parseProtocolEvents = parseProtocolEvents;
exports.findPending = findPending;
exports.buildStdin = buildStdin;
exports.parsePendingDecision = parsePendingDecision;
const worker_threads_1 = require("worker_threads");
const path = __importStar(require("path"));
const compiler_1 = require("fil-compiler/dist/compiler");
exports.WASM_TIMEOUT_MS = 5000;
class FilCompileError extends Error {
    constructor(errors) {
        super(`FIL compilation failed (${errors.length} error${errors.length === 1 ? '' : 's'})`);
        this.errors = errors;
        this.name = 'FilCompileError';
    }
}
exports.FilCompileError = FilCompileError;
class WasmTimeoutError extends Error {
    constructor(timeoutMs) {
        super(`WASM execution exceeded ${timeoutMs}ms timeout. ` +
            `A single FIL replay is CPU-bound and shouldn't take this long; ` +
            `you likely have a runaway loop or pathological recursion.`);
        this.timeoutMs = timeoutMs;
        this.name = 'WasmTimeoutError';
    }
}
exports.WasmTimeoutError = WasmTimeoutError;
async function watToWasm(watText) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const WabtFactory = require('wabt');
    const wabt = await WabtFactory();
    const module = wabt.parseWat('flow.wat', watText, { bulk_memory: true });
    const binary = module.toBinary({ write_debug_names: false });
    module.destroy();
    return binary.buffer;
}
async function compileFil(filSource) {
    const result = (0, compiler_1.compile)(filSource);
    if (result.errorDetails.length > 0) {
        throw new FilCompileError(result.errorDetails);
    }
    return await watToWasm(result.wat);
}
async function runWasm(wasmBytes, stdinText, opts = {}) {
    const timeoutMs = opts.timeoutMs ?? exports.WASM_TIMEOUT_MS;
    // The worker file ships at dist/wasi-worker.js next to dist/wasi-host.js.
    const workerPath = path.join(__dirname, 'wasi-worker.js');
    return new Promise((resolve, reject) => {
        const worker = new worker_threads_1.Worker(workerPath, {
            workerData: { wasmBytes, stdinText },
        });
        let settled = false;
        const settle = (cb) => {
            if (settled)
                return;
            settled = true;
            cb();
        };
        const timer = setTimeout(() => {
            settle(() => {
                worker.terminate().catch(() => { });
                reject(new WasmTimeoutError(timeoutMs));
            });
        }, timeoutMs);
        worker.on('message', (msg) => {
            clearTimeout(timer);
            settle(() => {
                worker.terminate().catch(() => { });
                if (msg.ok) {
                    resolve({ stdout: msg.stdout ?? '', exitCode: msg.exitCode ?? 0 });
                }
                else {
                    reject(new Error(`WASM worker error: ${msg.error}`));
                }
            });
        });
        worker.on('error', (err) => {
            clearTimeout(timer);
            settle(() => {
                worker.terminate().catch(() => { });
                reject(err);
            });
        });
        worker.on('exit', (code) => {
            clearTimeout(timer);
            // If we already resolved/rejected, this is just bookkeeping. If not
            // and the worker exited unexpectedly, surface that.
            settle(() => {
                if (code !== 0) {
                    reject(new Error(`WASM worker exited unexpectedly with code ${code}`));
                }
                else {
                    // Shouldn't happen — worker always posts a message before exit.
                    reject(new Error('WASM worker exited without posting a result'));
                }
            });
        });
    });
}
function parseProtocolEvents(stdout) {
    const lines = stdout.split('\n');
    const events = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        // Group markers — they sit on their own line at the start of a group.
        let m = line.match(/^all: (\d+)$/);
        if (m) {
            events.push({ kind: 'all', n: parseInt(m[1], 10) });
            continue;
        }
        m = line.match(/^race: (\d+)$/);
        if (m) {
            events.push({ kind: 'race', n: parseInt(m[1], 10) });
            continue;
        }
        if (line === 'flowCompleted:') {
            const next = lines[i + 1] ?? '';
            const success = next.trim() === 'success: true';
            events.push({ kind: 'flowCompleted', success });
            if (success)
                i++;
            continue;
        }
        if (line === 'executeNode:') {
            const nameLine = lines[i + 1] ?? '';
            const inputHeader = lines[i + 2] ?? '';
            const nm = nameLine.match(/^  name: (\S+)$/);
            if (!nm)
                continue;
            const name = nm[1];
            let input = '{}';
            if (inputHeader === '  input: |') {
                const block = [];
                let j = i + 3;
                while (j < lines.length && lines[j].startsWith('    ')) {
                    block.push(lines[j].slice(4));
                    j++;
                }
                input = block.join('\n').trim() || '{}';
                i = j; // continue after the block (the trailing blank line is fine)
            }
            else {
                i += 1;
            }
            events.push({ kind: 'executeNode', name, input });
            continue;
        }
        if (line === 'waitOnTrigger:') {
            const triggerLine = lines[i + 1] ?? '';
            const tm = triggerLine.match(/^  trigger: (\S+)$/);
            if (!tm)
                continue;
            events.push({ kind: 'waitOnTrigger', name: tm[1] });
            i += 1;
            continue;
        }
        if (line === 'executeTimer:') {
            const detail = lines[i + 1] ?? '';
            const dur = detail.match(/^  duration: (\d+)$/);
            const dl = detail.match(/^  deadline: (\S+)$/);
            if (dur) {
                events.push({ kind: 'executeTimer', durationMs: parseInt(dur[1], 10) });
            }
            else if (dl) {
                events.push({ kind: 'executeTimer', deadline: dl[1] });
            }
            i += 1;
            continue;
        }
    }
    return events;
}
function findPending(stdout, history) {
    const events = parseProtocolEvents(stdout);
    const flowCompleted = events.some(e => e.kind === 'flowCompleted');
    // Walk events vs. history. Each history event consumes exactly one event
    // from the protocol stream.
    let h = 0;
    for (let i = 0; i < events.length; i++) {
        const ev = events[i];
        if (ev.kind === 'flowCompleted') {
            // History should be fully consumed by now.
            return null;
        }
        if (h >= history.length) {
            return { index: i, event: ev, events, flowCompleted };
        }
        if (matches(ev, history[h])) {
            h++;
            continue;
        }
        // Mismatch — should not happen with correct history; surface it.
        return { index: i, event: ev, events, flowCompleted };
    }
    // All events matched and no flowCompleted? That means the WASM exited
    // cleanly without emitting anything new — caller decides what to do.
    return null;
}
function matches(ev, h) {
    switch (ev.kind) {
        case 'executeNode': return (h.kind === 'node' && h.name === ev.name) || h.kind === 'node-cancelled';
        case 'waitOnTrigger': return h.kind === 'trigger-fired' && h.name === ev.name;
        case 'executeTimer': return h.kind === 'timer' || h.kind === 'timer-cancelled';
        case 'all': return h.kind === 'all-marker' && h.n === ev.n;
        case 'race': return h.kind === 'race-marker' && h.n === ev.n;
        case 'flowCompleted': return false;
    }
}
/**
 * Build the stdin text the FIL replay parser will consume.
 *
 *   node:           → "node: <name>\n  output: <json>\n"
 *   trigger-fired:  → "triggerEvent: <name>\n  payload: <json>\n"
 *   timer:          → "timer: <ISO>\n"
 *   all-marker:     → "all: N\n"
 *   race-marker:    → "race: N winner=K\n"
 *   node-cancelled: → "nodeCancelled:\n"
 *   timer-cancelled:→ "timerCancelled:\n"
 *
 * Order matters — events must match the sequence FIL emits when re-run.
 */
function buildStdin(events) {
    return events.map(e => {
        switch (e.kind) {
            case 'node': return `node: ${e.name}\n  output: ${e.output}\n`;
            case 'trigger-fired': return `triggerEvent: ${e.name}\n  payload: ${e.payload}\n`;
            case 'timer': return `timer: ${e.deadline}\n`;
            case 'all-marker': return `all: ${e.n}\n`;
            case 'race-marker': return `race: ${e.n} winner=${e.winner}\n`;
            case 'node-cancelled': return `nodeCancelled:\n`;
            case 'timer-cancelled': return `timerCancelled:\n`;
        }
    }).join('');
}
function parsePendingDecision(stdout) {
    if (stdout.includes('flowCompleted:'))
        return null;
    const events = parseProtocolEvents(stdout);
    for (let i = events.length - 1; i >= 0; i--) {
        const ev = events[i];
        if (ev.kind === 'executeNode')
            return { kind: 'executeNode', name: ev.name };
        if (ev.kind === 'waitOnTrigger')
            return { kind: 'waitOnTrigger', name: ev.name };
        if (ev.kind === 'executeTimer')
            return { kind: 'executeTimer', durationMs: ev.durationMs, deadline: ev.deadline };
    }
    return null;
}
//# sourceMappingURL=wasi-host.js.map