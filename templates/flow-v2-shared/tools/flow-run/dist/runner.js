"use strict";
// The replay loop: compile FIL once, then repeatedly run WASM with the
// accumulated history, find the next pending protocol event, dispatch it,
// and append the result(s).
//
// Decisions can be solo (executeNode / executeTimer) or grouped — the
// FIL Promise.all and Promise.any constructs emit `all: N` / `race: N`
// markers followed by N entries that the host must dispatch together.
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
exports.runFlow = runFlow;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const wasi_host_1 = require("./wasi-host");
const dispatcher_1 = require("./dispatcher");
const history_1 = require("./history");
async function runFlow(project, resolved, opts) {
    const byNodeId = new Map();
    for (const r of resolved)
        byNodeId.set(r.nodeId, r);
    const wasm = await (0, wasi_host_1.compileFil)(project.filSource);
    let history;
    if (opts.resume) {
        const loaded = (0, history_1.loadHistory)(opts.historyPath);
        if (!loaded) {
            history = (0, history_1.newHistory)(project.flowId, project.flowName);
        }
        else {
            if (loaded.flowId !== project.flowId) {
                throw new Error(`--resume: history file is for flowId "${loaded.flowId}", ` +
                    `but project has flowId "${project.flowId}"`);
            }
            history = loaded;
            log(opts, `[resume] continuing from ${history.events.length} prior events`);
        }
    }
    else {
        history = (0, history_1.newHistory)(project.flowId, project.flowName);
    }
    const decisions = [];
    for (let step = 1; step <= opts.maxSteps; step++) {
        const stdin = (0, wasi_host_1.buildStdin)(history.events);
        const { stdout, exitCode } = await (0, wasi_host_1.runWasm)(wasm, stdin);
        if (opts.verbose) {
            log(opts, `[step ${step}] wasm exit=${exitCode} history=${history.events.length}`);
        }
        const pending = (0, wasi_host_1.findPending)(stdout, history.events);
        if (!pending) {
            if (!stdout.includes('flowCompleted:')) {
                writeDecisions(opts, project, history, decisions, false);
                return {
                    success: false,
                    steps: step,
                    history,
                    decisions,
                    error: `wasm exited at step ${step} without flowCompleted and without a pending event. ` +
                        `stdout: ${stdout.slice(0, 500)}`,
                };
            }
            log(opts, `[done] flowCompleted in ${history.events.length} events`);
            writeDecisions(opts, project, history, decisions, true);
            return { success: true, steps: step, history, decisions };
        }
        // ── Group: Promise.all ─────────────────────────────────────────────────
        if (pending.event.kind === 'all') {
            const n = pending.event.n;
            const entries = pending.events.slice(pending.index + 1, pending.index + 1 + n);
            if (entries.length !== n) {
                return failWith(opts, project, history, decisions, step, `Promise.all marker said N=${n} but stdout only has ${entries.length} entries after it`);
            }
            const groupRecords = await dispatchGroupAll(entries, byNodeId, decisions, step, opts);
            if (groupRecords === null) {
                // dispatch failure — error already recorded, return failure
                writeDecisions(opts, project, history, decisions, false);
                return {
                    success: false, steps: step, history, decisions,
                    error: decisions[decisions.length - 1]?.output && decisions[decisions.length - 1].output.error
                        ? String(decisions[decisions.length - 1].output.error)
                        : `Promise.all entry failed at step ${step}`,
                };
            }
            (0, history_1.appendEvent)(history, { kind: 'all-marker', n });
            for (const r of groupRecords)
                (0, history_1.appendEvent)(history, r);
            (0, history_1.saveHistory)(opts.historyPath, history);
            writeDecisions(opts, project, history, decisions, false);
            continue;
        }
        // ── Group: Promise.any (race) ──────────────────────────────────────────
        if (pending.event.kind === 'race') {
            const n = pending.event.n;
            const entries = pending.events.slice(pending.index + 1, pending.index + 1 + n);
            if (entries.length !== n) {
                return failWith(opts, project, history, decisions, step, `Promise.any marker said N=${n} but stdout only has ${entries.length} entries after it`);
            }
            const raceResult = await dispatchGroupRace(entries, byNodeId, decisions, step, opts);
            if (raceResult === null) {
                writeDecisions(opts, project, history, decisions, false);
                return {
                    success: false, steps: step, history, decisions,
                    error: `Promise.any: all ${n} entries failed at step ${step}`,
                };
            }
            (0, history_1.appendEvent)(history, { kind: 'race-marker', n, winner: raceResult.winner });
            for (const r of raceResult.records)
                (0, history_1.appendEvent)(history, r);
            (0, history_1.saveHistory)(opts.historyPath, history);
            writeDecisions(opts, project, history, decisions, false);
            continue;
        }
        // ── Solo: executeTimer ─────────────────────────────────────────────────
        if (pending.event.kind === 'executeTimer') {
            const result = handleTimer(pending.event, opts);
            log(opts, `[step ${step}] -> timer (deadline ${result.deadline})`);
            if (opts.realTime && result.sleepMs > 0) {
                log(opts, `[step ${step}]    sleeping ${result.sleepMs}ms`);
                await new Promise(r => setTimeout(r, result.sleepMs));
            }
            decisions.push({
                step,
                nodeId: '<timer>',
                nodeType: 'core.logic.delay',
                kind: 'timer',
                input: pending.event.durationMs !== undefined
                    ? { durationMs: pending.event.durationMs }
                    : { deadline: pending.event.deadline },
                output: { deadline: result.deadline },
                dispatchedAt: new Date().toISOString(),
                durationMs: opts.realTime ? result.sleepMs : 0,
            });
            (0, history_1.appendEvent)(history, { kind: 'timer', deadline: result.deadline });
            (0, history_1.saveHistory)(opts.historyPath, history);
            writeDecisions(opts, project, history, decisions, false);
            continue;
        }
        // ── Solo: executeNode ──────────────────────────────────────────────────
        if (pending.event.kind !== 'executeNode') {
            // Should be unreachable — flowCompleted handled above, all/race/timer
            // handled in their branches.
            return failWith(opts, project, history, decisions, step, `unhandled protocol event kind: ${pending.event.kind}`);
        }
        const ev = pending.event;
        const node = byNodeId.get(ev.name);
        if (!node) {
            writeDecisions(opts, project, history, decisions, false);
            return {
                success: false, steps: step, history, decisions,
                error: `pending decision references node "${ev.name}", but pre-flight did not resolve a ` +
                    `dispatchable node for it. The FIL may be missing an \`action ${ev.name}: …\` declaration, ` +
                    `the node may be a non-CRUD connector activity (Download/Upload — not yet supported), or ` +
                    `the node is a subflow/control-flow type that should not produce a decision.`,
            };
        }
        const inputJson = ev.input;
        logNodeStart(opts, step, ev.name, node);
        const dispatchedAt = new Date().toISOString();
        const t0 = Date.now();
        let outputJson;
        let outputParsed;
        try {
            const result = await (0, dispatcher_1.dispatch)(node, inputJson, {
                dryRun: opts.dryRun, verbose: opts.verbose, tenant: opts.tenant,
                history: history.events,
            });
            outputJson = result.outputJson;
            try {
                outputParsed = JSON.parse(outputJson);
            }
            catch {
                outputParsed = outputJson;
            }
        }
        catch (e) {
            const err = e;
            const detail = err.envelope ? ` envelope=${JSON.stringify(err.envelope)}` : '';
            const stderrTail = err.stderr ? ` stderr=${err.stderr.slice(-300)}` : '';
            decisions.push(buildDecisionRecord(step, ev.name, node, inputJson, err.envelope ?? null, dispatchedAt, Date.now() - t0));
            writeDecisions(opts, project, history, decisions, false);
            return {
                success: false, steps: step, history, decisions,
                error: `${err.message}${detail}${stderrTail}`,
            };
        }
        decisions.push(buildDecisionRecord(step, ev.name, node, inputJson, outputParsed, dispatchedAt, Date.now() - t0));
        (0, history_1.appendEvent)(history, { kind: 'node', name: ev.name, output: outputJson });
        (0, history_1.saveHistory)(opts.historyPath, history);
        writeDecisions(opts, project, history, decisions, false);
    }
    writeDecisions(opts, project, history, decisions, false);
    return {
        success: false, steps: opts.maxSteps, history, decisions,
        error: `max-steps (${opts.maxSteps}) reached without flowCompleted`,
    };
}
/**
 * Promise.all: every entry must complete. Returns the history records to
 * append (in entry order), or null if any entry failed.
 */
async function dispatchGroupAll(entries, byNodeId, decisions, step, opts) {
    log(opts, `[step ${step}] -> Promise.all (${entries.length} entries)`);
    const settled = await Promise.all(entries.map((ev, i) => settleEntry(ev, i, byNodeId, opts)));
    // Record each entry as a decision (success or failure)
    let anyFailed = false;
    for (const s of settled) {
        decisions.push(buildEntryDecisionRecord(step, s, opts));
        if (!s.ok)
            anyFailed = true;
    }
    if (anyFailed)
        return null;
    return settled.map(toHistoryEvent);
}
/**
 * Promise.any: dispatch all concurrently, the first to settle wins. Returns
 * { winner, records } or null if every entry failed.
 *
 * Race semantics:
 *   - All entries dispatched concurrently.
 *   - Without --real-time, executeTimer entries resolve their deadline
 *     immediately (no sleep) — they typically beat real I/O. With
 *     --real-time, timers actually sleep and the fastest path wins.
 *   - First entry to settle (resolve OR reject) is the winner; if the
 *     winner rejected, we try the next-fastest until one succeeds.
 *   - Losers are recorded as nodeCancelled / timerCancelled.
 */
async function dispatchGroupRace(entries, byNodeId, decisions, step, opts) {
    log(opts, `[step ${step}] -> Promise.any (${entries.length} entries)`);
    // Mirror JS Promise.any semantics: the first entry to fulfill (resolve)
    // wins; rejections are deferred and only matter if every entry rejects.
    // We must NOT await rejected/slow entries past the winner — for a 1-hour
    // SLA timer racing real I/O, blocking on the timer would defeat the point.
    const settled = new Array(entries.length);
    let resolved = 0;
    const winner = await new Promise(resolveOuter => {
        let outerResolved = false;
        let rejections = 0;
        entries.forEach((ev, i) => {
            settleEntry(ev, i, byNodeId, opts).then(s => {
                settled[i] = s;
                resolved++;
                if (s.ok && !outerResolved) {
                    outerResolved = true;
                    resolveOuter(s);
                    return;
                }
                if (!s.ok) {
                    rejections++;
                    if (rejections === entries.length && !outerResolved) {
                        outerResolved = true;
                        resolveOuter(null);
                    }
                }
            });
        });
    });
    // Record decisions for every entry that settled by the time the winner
    // emerged. Pending entries are recorded as orphaned (output: null).
    for (let i = 0; i < entries.length; i++) {
        if (settled[i]) {
            decisions.push(buildEntryDecisionRecord(step, settled[i], opts));
        }
        else {
            // Entry hadn't settled when the winner was decided — orphaned.
            const ev = entries[i];
            let nodeId = '<unknown>';
            let nodeType = '<group>';
            let kind = 'connector';
            let input = {};
            if (ev.kind === 'executeNode') {
                nodeId = ev.name;
                input = tryParseJson(ev.input);
            }
            else if (ev.kind === 'executeTimer') {
                nodeId = '<timer>';
                nodeType = 'core.logic.delay';
                kind = 'timer';
                input = ev.durationMs !== undefined ? { durationMs: ev.durationMs } : { deadline: ev.deadline };
            }
            decisions.push({
                step, nodeId, nodeType, kind, input,
                output: null,
                dispatchedAt: new Date().toISOString(),
                durationMs: 0,
            });
        }
    }
    if (!winner)
        return null;
    const winnerIdx = winner.index;
    const records = entries.map((ev, i) => {
        if (i === winnerIdx)
            return toHistoryEvent(winner);
        return ev.kind === 'executeNode' ? { kind: 'node-cancelled' } : { kind: 'timer-cancelled' };
    });
    return { winner: winnerIdx, records };
}
async function settleEntry(ev, index, byNodeId, opts) {
    const dispatchedAt = new Date().toISOString();
    const t0 = Date.now();
    if (ev.kind === 'executeTimer') {
        const r = handleTimer(ev, opts);
        if (opts.realTime && r.sleepMs > 0) {
            await new Promise(res => setTimeout(res, r.sleepMs));
        }
        return {
            index, ok: true,
            result: { outputJson: JSON.stringify({ deadline: r.deadline }), raw: '<timer>' },
            isNode: false,
            timerDeadline: r.deadline,
            dispatchedAt,
            durationMs: opts.realTime ? r.sleepMs : 0,
        };
    }
    if (ev.kind !== 'executeNode') {
        return {
            index, ok: false, isNode: false, dispatchedAt, durationMs: 0,
            error: new dispatcher_1.DispatchError(`group entry ${index}: unsupported kind ${ev.kind}`, '<group>'),
        };
    }
    const node = byNodeId.get(ev.name);
    if (!node) {
        return {
            index, ok: false, isNode: true, nodeName: ev.name, inputJson: ev.input,
            dispatchedAt, durationMs: 0,
            error: new dispatcher_1.DispatchError(`group entry ${index} references node "${ev.name}", which pre-flight did not resolve`, ev.name),
        };
    }
    try {
        const result = await (0, dispatcher_1.dispatch)(node, ev.input, {
            dryRun: opts.dryRun, verbose: opts.verbose, tenant: opts.tenant,
        }); // group entries don't currently get $vars context — scripts in groups are rare
        return {
            index, ok: true, result,
            isNode: true, nodeName: ev.name, inputJson: ev.input,
            dispatchedAt, durationMs: Date.now() - t0,
        };
    }
    catch (e) {
        return {
            index, ok: false, isNode: true, nodeName: ev.name, inputJson: ev.input,
            dispatchedAt, durationMs: Date.now() - t0,
            error: e,
        };
    }
}
function toHistoryEvent(s) {
    if (!s.ok || !s.result) {
        // Caller should have caught this; defensive fallback.
        if (s.isNode && s.nodeName)
            return { kind: 'node', name: s.nodeName, output: '{}' };
        return { kind: 'timer', deadline: s.timerDeadline ?? new Date().toISOString() };
    }
    if (s.isNode && s.nodeName)
        return { kind: 'node', name: s.nodeName, output: s.result.outputJson };
    return { kind: 'timer', deadline: s.timerDeadline ?? new Date().toISOString() };
}
function buildEntryDecisionRecord(step, s, opts) {
    if (s.isNode && s.nodeName) {
        return {
            step,
            nodeId: s.nodeName,
            nodeType: '<group>',
            kind: 'connector', // approximated; real kind lives in resolved
            input: s.inputJson ? tryParseJson(s.inputJson) : {},
            output: s.ok && s.result ? tryParseJson(s.result.outputJson) : (s.error?.envelope ?? null),
            dispatchedAt: s.dispatchedAt,
            durationMs: s.durationMs,
        };
    }
    return {
        step,
        nodeId: '<timer>',
        nodeType: 'core.logic.delay',
        kind: 'timer',
        input: {},
        output: { deadline: s.timerDeadline },
        dispatchedAt: s.dispatchedAt,
        durationMs: s.durationMs,
    };
}
// ─── Solo helpers (carried over) ────────────────────────────────────────────
function buildDecisionRecord(step, nodeId, node, inputJson, output, dispatchedAt, durationMs) {
    const base = {
        step,
        nodeId,
        nodeType: node.nodeType,
        kind: node.kind,
        input: tryParseJson(inputJson),
        output,
        dispatchedAt,
        durationMs,
    };
    if (node.kind === 'connector') {
        return { ...base, connectorKey: node.connectorKey, verb: node.verb, objectName: node.objectName };
    }
    if (node.kind === 'agent' || node.kind === 'api-workflow' || node.kind === 'rpa-workflow') {
        return {
            ...base,
            resourceKey: node.resourceKey,
            resourceSubType: node.resourceSubType,
            serviceType: node.serviceType,
            name: node.name,
            folderPath: node.folderPath,
        };
    }
    if (node.kind === 'inline-agent') {
        return {
            ...base,
            source: node.source,
            serviceType: node.serviceType,
        };
    }
    return base;
}
function logNodeStart(opts, step, nodeId, node) {
    if (!opts.verbose)
        return;
    let detail;
    switch (node.kind) {
        case 'connector':
            detail = `${node.connectorKey} ${node.verb} ${node.objectName}`;
            break;
        case 'http':
            detail = 'HTTP';
            break;
        case 'mock':
            detail = 'mock';
            break;
        case 'script':
            detail = 'script';
            break;
        case 'agent':
            detail = `agent ${node.name || node.resourceKey || ''}`.trim();
            break;
        case 'api-workflow':
            detail = `api-workflow ${node.name || node.resourceKey || ''}`.trim();
            break;
        case 'rpa-workflow':
            detail = `rpa-workflow ${node.name || node.resourceKey || ''}`.trim();
            break;
        case 'inline-agent':
            detail = `inline-agent ${node.source}`;
            break;
        case 'hitl':
            detail = `hitl ${node.taskType}`;
            break;
    }
    log(opts, `[step ${step}] -> ${nodeId} (${detail})`);
}
function handleTimer(p, _opts) {
    if (p.deadline) {
        const target = Date.parse(p.deadline);
        const sleepMs = Number.isFinite(target) ? Math.max(0, target - Date.now()) : 0;
        return { deadline: p.deadline, sleepMs };
    }
    if (p.durationMs !== undefined) {
        const target = Date.now() + p.durationMs;
        return { deadline: new Date(target).toISOString(), sleepMs: p.durationMs };
    }
    return { deadline: new Date().toISOString(), sleepMs: 0 };
}
function failWith(opts, project, history, decisions, step, msg) {
    writeDecisions(opts, project, history, decisions, false);
    return {
        success: false, steps: step, history, decisions, error: msg,
    };
}
function writeDecisions(opts, project, history, decisions, success) {
    const file = {
        runId: history.runId,
        flowId: project.flowId,
        flowName: project.flowName,
        startedAt: history.startedAt,
        finishedAt: success ? new Date().toISOString() : undefined,
        success,
        decisions,
    };
    fs.mkdirSync(path.dirname(opts.decisionsPath), { recursive: true });
    fs.writeFileSync(opts.decisionsPath, JSON.stringify(file, null, 2) + '\n', 'utf8');
}
function tryParseJson(s) {
    try {
        return JSON.parse(s);
    }
    catch {
        return s;
    }
}
function log(opts, msg) {
    if (opts.verbose)
        process.stderr.write(`${msg}\n`);
}
//# sourceMappingURL=runner.js.map