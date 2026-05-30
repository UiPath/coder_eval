"use strict";
// History persistence. The on-disk file is human-readable YAML-ish, but the
// FIL replay parser only sees the events serialized by buildStdin (which uses
// the strict `node:`/`output:` two-line format). Persisting metadata (runId,
// flowId, etc.) at the top of the file is for humans/diff tooling only.
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
exports.loadHistory = loadHistory;
exports.saveHistory = saveHistory;
exports.newHistory = newHistory;
exports.appendEvent = appendEvent;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const GITIGNORE_TAG = '# added by flow-run';
function loadHistory(historyPath) {
    if (!fs.existsSync(historyPath))
        return null;
    const text = fs.readFileSync(historyPath, 'utf8');
    try {
        return parseHistory(text);
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        throw new Error(`failed to parse history file ${historyPath}: ${msg}`);
    }
}
function saveHistory(historyPath, history) {
    ensureGitignored(historyPath);
    fs.mkdirSync(path.dirname(historyPath), { recursive: true });
    fs.writeFileSync(historyPath, serializeHistory(history), 'utf8');
}
function newHistory(flowId, flowName) {
    return {
        runId: cryptoRandomId(),
        flowId,
        flowName,
        startedAt: new Date().toISOString(),
        events: [],
    };
}
function appendEvent(history, event) {
    if (event.kind === 'node' && /[\r\n]/.test(event.output)) {
        throw new Error(`node "${event.name}" output contains newlines, which would break the FIL replay parser. ` +
            `Outputs must be single-line JSON.`);
    }
    if (event.kind === 'trigger-fired' && /[\r\n]/.test(event.payload)) {
        throw new Error(`trigger "${event.name}" payload contains newlines, which would break the FIL replay parser. ` +
            `Payloads must be single-line JSON.`);
    }
    history.events.push(event);
}
// ─── Serialization ───────────────────────────────────────────────────────────
function serializeHistory(h) {
    const header = `# flow-run history\n` +
        `runId: ${h.runId}\n` +
        `flowId: ${h.flowId}\n` +
        `flowName: ${h.flowName}\n` +
        `startedAt: ${h.startedAt}\n` +
        `# events follow (one wire-protocol line/block per event)\n`;
    const body = h.events.map(e => {
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
    return header + body;
}
function parseHistory(text) {
    const lines = text.split('\n');
    let runId = '', flowId = '', flowName = '', startedAt = '';
    const events = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('#') || line.trim() === '')
            continue;
        let m;
        if ((m = line.match(/^runId:\s*(.+)$/))) {
            runId = m[1].trim();
            continue;
        }
        if ((m = line.match(/^flowId:\s*(.+)$/))) {
            flowId = m[1].trim();
            continue;
        }
        if ((m = line.match(/^flowName:\s*(.+)$/))) {
            flowName = m[1].trim();
            continue;
        }
        if ((m = line.match(/^startedAt:\s*(.+)$/))) {
            startedAt = m[1].trim();
            continue;
        }
        if ((m = line.match(/^node:\s*(\S+)\s*$/))) {
            const name = m[1];
            const next = lines[i + 1] ?? '';
            const om = next.match(/^\s\s+output:\s*(.*)$/);
            if (!om)
                throw new Error(`expected "  output: ..." after "node: ${name}"`);
            events.push({ kind: 'node', name, output: om[1] });
            i++;
            continue;
        }
        if ((m = line.match(/^triggerEvent:\s*(\S+)\s*$/))) {
            const name = m[1];
            const next = lines[i + 1] ?? '';
            const pm = next.match(/^\s\s+payload:\s*(.*)$/);
            if (!pm)
                throw new Error(`expected "  payload: ..." after "triggerEvent: ${name}"`);
            events.push({ kind: 'trigger-fired', name, payload: pm[1] });
            i++;
            continue;
        }
        if ((m = line.match(/^timer:\s*(\S+)\s*$/))) {
            events.push({ kind: 'timer', deadline: m[1] });
            continue;
        }
        if ((m = line.match(/^all:\s*(\d+)\s*$/))) {
            events.push({ kind: 'all-marker', n: parseInt(m[1], 10) });
            continue;
        }
        if ((m = line.match(/^race:\s*(\d+)\s+winner=(\d+)\s*$/))) {
            events.push({ kind: 'race-marker', n: parseInt(m[1], 10), winner: parseInt(m[2], 10) });
            continue;
        }
        if (line === 'nodeCancelled:') {
            events.push({ kind: 'node-cancelled' });
            continue;
        }
        if (line === 'timerCancelled:') {
            events.push({ kind: 'timer-cancelled' });
            continue;
        }
        // Unknown line: ignore (forward-compat).
    }
    return { runId, flowId, flowName, startedAt, events };
}
// ─── .gitignore management ───────────────────────────────────────────────────
function ensureGitignored(historyPath) {
    // Walk upward looking for the project's .gitignore (or repo root). We add
    // `.flow-run/` to the nearest .gitignore if present, or create one in the
    // history file's parent directory.
    const dir = path.dirname(historyPath);
    const projectRoot = findProjectRoot(dir);
    const gitignore = path.join(projectRoot, '.gitignore');
    let existing = '';
    try {
        existing = fs.readFileSync(gitignore, 'utf8');
    }
    catch { /* missing is fine */ }
    if (existing.split('\n').some(l => l.trim() === '.flow-run/' || l.trim() === '.flow-run'))
        return;
    const addition = (existing.endsWith('\n') || existing === '' ? '' : '\n') +
        `${GITIGNORE_TAG}\n.flow-run/\n`;
    try {
        fs.writeFileSync(gitignore, existing + addition, 'utf8');
    }
    catch {
        // Best-effort; if we can't write .gitignore (read-only checkout, etc.)
        // we still proceed. The user can add it themselves.
    }
}
function findProjectRoot(start) {
    let cur = path.resolve(start);
    while (true) {
        if (fs.existsSync(path.join(cur, '.git')))
            return cur;
        const parent = path.dirname(cur);
        if (parent === cur)
            return start; // fall back to where we started
        cur = parent;
    }
}
function cryptoRandomId() {
    // 8 bytes of hex is plenty for log correlation.
    // Avoid `crypto.randomUUID` to keep Node 16 compatibility.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { randomBytes } = require('crypto');
    return randomBytes(8).toString('hex');
}
//# sourceMappingURL=history.js.map