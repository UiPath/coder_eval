"use strict";
// Dispatches a decision (executeNode) to the appropriate backend based on
// the resolved node's `kind`. Returns the response as a single-line JSON
// string for feeding back into FIL history.
//
// Dispatch backends:
//   connector → `uip is resources execute <verb> ...`
//   http      → Node fetch() against the input URL
//   mock      → return the configured fixture (or {})
//   agent     → dry-run fixture only; live Agent dispatch is not wired yet
//   api-workflow → dry-run fixture only; live API Workflow dispatch is not wired yet
//   rpa-workflow → dry-run fixture only; live RPA Workflow dispatch is not wired yet
//   agentic-process → dry-run fixture only; live Agentic Process dispatch is not wired yet
//   summarize → dry-run fixture only; live ECS.DeepRag dispatch is not wired yet
//   batch-transform → dry-run fixture only; live ECS.BatchTransform dispatch is not wired yet
//   queue    → dry-run fixture only; live Orchestrator Queue dispatch is not wired yet
Object.defineProperty(exports, "__esModule", { value: true });
exports.DispatchError = void 0;
exports.dispatch = dispatch;
exports.dispatchConnector = dispatchConnector;
exports.dispatchHttp = dispatchHttp;
exports.mergeHttpInputs = mergeHttpInputs;
exports.dispatchMock = dispatchMock;
exports.dispatchAgent = dispatchAgent;
exports.dispatchApiWorkflow = dispatchApiWorkflow;
exports.dispatchRpaWorkflow = dispatchRpaWorkflow;
exports.dispatchAgenticProcess = dispatchAgenticProcess;
exports.dispatchInlineAgent = dispatchInlineAgent;
exports.dispatchHitl = dispatchHitl;
exports.dispatchSummarize = dispatchSummarize;
exports.dispatchBatchTransform = dispatchBatchTransform;
exports.dispatchQueue = dispatchQueue;
exports.dispatchScript = dispatchScript;
const child_process_1 = require("child_process");
class DispatchError extends Error {
    constructor(message, nodeId, stderr, envelope) {
        super(message);
        this.nodeId = nodeId;
        this.stderr = stderr;
        this.envelope = envelope;
        this.name = 'DispatchError';
    }
}
exports.DispatchError = DispatchError;
async function dispatch(node, inputJson, opts) {
    switch (node.kind) {
        case 'connector': return dispatchConnector(node, inputJson, opts);
        case 'http': return await dispatchHttp(node, inputJson, opts);
        case 'mock': return dispatchMock(node, inputJson, opts);
        case 'script': return dispatchScript(node, inputJson, opts);
        case 'agent': return dispatchAgent(node, inputJson, opts);
        case 'api-workflow': return dispatchApiWorkflow(node, inputJson, opts);
        case 'rpa-workflow': return dispatchRpaWorkflow(node, inputJson, opts);
        case 'agentic-process': return dispatchAgenticProcess(node, inputJson, opts);
        case 'inline-agent': return dispatchInlineAgent(node, inputJson, opts);
        case 'hitl': return dispatchHitl(node, inputJson, opts);
        case 'summarize': return dispatchSummarize(node, inputJson, opts);
        case 'batch-transform': return dispatchBatchTransform(node, inputJson, opts);
        case 'queue': return dispatchQueue(node, inputJson, opts);
    }
}
// ─── Connector dispatch (existing) ───────────────────────────────────────────
function dispatchConnector(node, inputJson, opts) {
    const args = buildConnectorArgs(node, inputJson, opts);
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] uip ${args.join(' ')}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: dryRunConnectorMock(node), raw: '<dry-run>' };
    }
    const proc = (0, child_process_1.spawnSync)('uip', args, { encoding: 'utf8' });
    if (proc.error) {
        throw new DispatchError(`failed to spawn 'uip': ${proc.error.message}`, node.nodeId);
    }
    if (proc.status !== 0) {
        throw new DispatchError(`uip exited ${proc.status} for node "${node.nodeId}"`, node.nodeId, proc.stderr || proc.stdout);
    }
    const stdout = (proc.stdout ?? '').trim();
    let parsed;
    try {
        parsed = JSON.parse(stdout);
    }
    catch {
        throw new DispatchError(`node "${node.nodeId}": uip stdout was not valid JSON`, node.nodeId, stdout.slice(0, 500));
    }
    if (isFailureEnvelope(parsed)) {
        throw new DispatchError(`node "${node.nodeId}" failed: ${parsed.Message ?? 'unknown'}`, node.nodeId, undefined, parsed);
    }
    return { outputJson: JSON.stringify(parsed), raw: stdout };
}
function buildConnectorArgs(node, inputJson, opts) {
    const args = ['is', 'resources', 'execute', node.verb, node.connectorKey, node.objectName,
        '--connection-id', node.connectionId,
        '--output', 'json'];
    if (opts.tenant)
        args.push('-t', opts.tenant);
    switch (node.verb) {
        case 'create':
        case 'update':
        case 'replace':
            args.push('--body', inputJson || '{}');
            break;
        case 'get':
        case 'list':
        case 'delete':
            if (inputJson && inputJson !== '{}' && inputJson !== '""') {
                args.push('--query', inputJson);
            }
            break;
    }
    return args;
}
function isFailureEnvelope(v) {
    if (typeof v !== 'object' || v === null)
        return false;
    const o = v;
    return o.Result === 'Failure' && typeof o.Message === 'string';
}
/**
 * Best-effort canned response for a connector node in dry-run mode. Returns a
 * single-line JSON string with shape that matches typical v1 connector output
 * envelopes — enough to keep FIL code that walks the response from
 * null-dereferencing every field. Real production output is much richer; the
 * goal here is structural, not faithful.
 *
 * NOTE: this is a degenerate-data fallback. The FIL runtime has known
 * defensiveness gaps when fed bare `{}` / missing fields (e.g. JSON.stringify
 * on an object whose values came from missing-field reads can crash inside
 * `$str_concat`). Returning a populated envelope sidesteps those gaps in the
 * dry-run path. See `docs/known-issues.md` for the underlying defensiveness
 * issue.
 */
function dryRunConnectorMock(node) {
    const base = {
        Id: `dry-run-${node.nodeId}`,
        Key: `dry-run-${node.nodeId}`,
        Name: `dry-run ${node.objectName}`,
    };
    // `list` operations return arrays — FIL code typically iterates them.
    if (node.verb === 'list') {
        return JSON.stringify({ value: [base] });
    }
    return JSON.stringify(base);
}
// ─── HTTP dispatch ──────────────────────────────────────────────────────────
async function dispatchHttp(node, inputJson, opts) {
    let filInput;
    try {
        const parsed = JSON.parse(inputJson);
        filInput = (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
            ? parsed
            : {};
    }
    catch {
        throw new DispatchError(`node "${node.nodeId}": HTTP input is not valid JSON`, node.nodeId, inputJson.slice(0, 500));
    }
    const input = mergeHttpInputs(node.rawInputs ?? {}, filInput);
    const url = input.url;
    if (!url) {
        throw new DispatchError(`node "${node.nodeId}": HTTP node missing "url" — not in the action's rawInputs and not provided by the executeNode call's input`, node.nodeId);
    }
    const method = (input.method ?? 'GET').toUpperCase();
    const timeoutMs = parseIsoDurationMs(input.timeout) ?? 60000;
    const retryCount = Math.max(0, Math.min(input.retryCount ?? 0, 5));
    const headers = parseHeaderObject(input.headers);
    const queryParams = parseHeaderObject(input.queryParams);
    const finalUrl = appendQueryParams(url, queryParams);
    const init = { method, headers };
    if (method !== 'GET' && method !== 'HEAD') {
        if (input.body !== undefined && input.body !== '') {
            init.body = typeof input.body === 'string' ? input.body : JSON.stringify(input.body);
            if (!('Content-Type' in headers) && input.contentType) {
                headers['Content-Type'] = input.contentType;
            }
        }
    }
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] HTTP ${method} ${finalUrl} (timeout ${timeoutMs}ms, retries ${retryCount})\n`);
    }
    if (opts.dryRun) {
        // Return the v1 HTTP envelope shape so downstream FIL code that does
        // `JSON.parse(raw).body.<field>` sees a real object (body={}), not
        // undefined-field-derefs that propagate through the rest of the flow.
        return {
            outputJson: JSON.stringify({ body: {}, headers: {}, statusCode: 200 }),
            raw: '<dry-run>',
        };
    }
    let attempt = 0;
    let lastError;
    while (attempt <= retryCount) {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), timeoutMs);
        try {
            const res = await fetch(finalUrl, { ...init, signal: ac.signal });
            clearTimeout(timer);
            const text = await res.text();
            const responseHeaders = {};
            res.headers.forEach((v, k) => { responseHeaders[k] = v; });
            // Try to parse the body as JSON (the FIL pattern is JSON.parse(raw)
            // downstream); fall back to the raw string if not JSON.
            let body;
            try {
                body = JSON.parse(text);
            }
            catch {
                body = text;
            }
            // Output mirrors v1's HTTP node shape: { output: { body, headers, statusCode } }
            // We skip the wrapping `output` envelope here — FIL's executeNode
            // returns the raw connector output, and downstream FIL code reads it
            // via JSON.parse. The manifest's `outputs` field maps fields from
            // the node output to flow variables; we keep parity with v1 by
            // returning { body, headers, statusCode } as the top-level shape.
            const out = { body, headers: responseHeaders, statusCode: res.status };
            if (!res.ok && attempt < retryCount && isRetryableStatus(res.status)) {
                attempt++;
                lastError = `HTTP ${res.status}`;
                continue;
            }
            return { outputJson: JSON.stringify(out), raw: text };
        }
        catch (e) {
            clearTimeout(timer);
            lastError = e;
            if (attempt < retryCount) {
                attempt++;
                continue;
            }
            const msg = e instanceof Error ? e.message : String(e);
            throw new DispatchError(`node "${node.nodeId}": HTTP ${method} ${finalUrl} failed: ${msg}`, node.nodeId);
        }
    }
    // Loop exhausted without returning
    throw new DispatchError(`node "${node.nodeId}": HTTP retries exhausted (last: ${String(lastError)})`, node.nodeId);
}
/**
 * Merge manifest-side `rawInputs` (statics) with FIL-side `executeNode`
 * inputs (dynamics). Per-field rules:
 *   - `headers`, `queryParams`: deep object merge (FIL keys override manifest
 *     keys; missing keys preserved from manifest).
 *   - `body`: FIL replaces wholesale (opaque payload).
 *   - All other fields (url, method, contentType, timeout, retryCount):
 *     FIL replaces if present, otherwise manifest value stands.
 *
 * Anything in `filInput` not in this list is also passed through unchanged
 * so we don't strip future extensions (e.g. auth, mtls config) at the merge
 * layer.
 */
function mergeHttpInputs(rawInputs, filInput) {
    const out = { ...rawInputs };
    for (const [k, v] of Object.entries(filInput)) {
        if (v === undefined)
            continue;
        if (k === 'headers' || k === 'queryParams') {
            out[k] = mergeObjectFields(rawInputs[k], v);
        }
        else {
            out[k] = v;
        }
    }
    return out;
}
function mergeObjectFields(base, override) {
    const merged = { ...parseHeaderObject(base) };
    const overrides = parseHeaderObject(override);
    for (const [k, v] of Object.entries(overrides))
        merged[k] = v;
    return merged;
}
function parseIsoDurationMs(d) {
    if (typeof d !== 'string')
        return null;
    // Minimal ISO 8601 duration parser: PT15M, PT1H, PT30S, P1DT2H, etc.
    const m = d.match(/^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/);
    if (!m)
        return null;
    const [, days, hours, minutes, seconds] = m;
    const ms = (parseInt(days ?? '0', 10) * 86400 +
        parseInt(hours ?? '0', 10) * 3600 +
        parseInt(minutes ?? '0', 10) * 60 +
        parseFloat(seconds ?? '0')) * 1000;
    return ms > 0 ? Math.round(ms) : null;
}
function parseHeaderObject(v) {
    if (!v)
        return {};
    // Headers/queryParams may arrive as a JSON-stringified string ("{}") or
    // an object literal. Both shapes appear in v1 fixtures.
    if (typeof v === 'string') {
        if (v === '' || v === '{}')
            return {};
        try {
            const parsed = JSON.parse(v);
            return typeof parsed === 'object' && parsed !== null
                ? Object.fromEntries(Object.entries(parsed).map(([k, vv]) => [k, String(vv)]))
                : {};
        }
        catch {
            return {};
        }
    }
    if (typeof v === 'object') {
        return Object.fromEntries(Object.entries(v).map(([k, vv]) => [k, String(vv)]));
    }
    return {};
}
function appendQueryParams(url, params) {
    const keys = Object.keys(params);
    if (keys.length === 0)
        return url;
    const sep = url.includes('?') ? '&' : '?';
    const qs = keys.map(k => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`).join('&');
    return url + sep + qs;
}
function isRetryableStatus(status) {
    return status === 408 || status === 429 || (status >= 500 && status < 600);
}
// ─── Mock dispatch ──────────────────────────────────────────────────────────
function dispatchMock(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] mock "${node.nodeId}" → ${node.fixture === undefined ? '{}' : 'fixture'}\n`);
    }
    // Mock fixtures arrive in two flavors: a JSON-stringified value (the v1
    // convention; rawInputs.fixture is a string like '{"category":"x"}') OR
    // an inline object/array. Detect and emit single-encoded JSON either way.
    // Without this check we'd double-encode a stringified fixture, and the
    // FIL JSON.parse downstream would return a bare string instead of the
    // intended object — leading to OOB memory access on field reads.
    let outputJson;
    if (node.fixture === undefined) {
        outputJson = '{}';
    }
    else if (typeof node.fixture === 'string') {
        const trimmed = node.fixture.trim();
        if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
            // Already JSON; pass through verbatim.
            outputJson = node.fixture;
        }
        else {
            // Literal string fixture — encode as a JSON string.
            outputJson = JSON.stringify(node.fixture);
        }
    }
    else {
        outputJson = JSON.stringify(node.fixture);
    }
    return { outputJson, raw: '<mock>' };
}
// ─── Agent dispatch ────────────────────────────────────────────────────────
function dispatchAgent(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const name = node.name || node.resourceKey || node.nodeId;
        process.stderr.write(`[dispatch] agent "${node.nodeId}" → ${name}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-agent>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live Agent dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for Orchestrator.StartAgentJob.`, node.nodeId);
}
function dispatchApiWorkflow(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const name = node.name || node.resourceKey || node.nodeId;
        process.stderr.write(`[dispatch] api-workflow "${node.nodeId}" → ${name}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-api-workflow>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live API Workflow dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for Orchestrator.ExecuteApiWorkflowAsync.`, node.nodeId);
}
function dispatchRpaWorkflow(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const name = node.name || node.resourceKey || node.nodeId;
        process.stderr.write(`[dispatch] rpa-workflow "${node.nodeId}" → ${name}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-rpa-workflow>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live RPA Workflow dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for Orchestrator.StartJob.`, node.nodeId);
}
function dispatchAgenticProcess(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const name = node.name || node.resourceKey || node.nodeId;
        process.stderr.write(`[dispatch] agentic-process "${node.nodeId}" → ${name}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-agentic-process>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live Agentic Process dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for ${node.serviceType}.`, node.nodeId);
}
function dispatchInlineAgent(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] inline-agent "${node.nodeId}" → ${node.source}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-inline-agent>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live inline Agent dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for Orchestrator.StartInlineAgentJob.`, node.nodeId);
}
function dispatchHitl(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] hitl "${node.nodeId}" -> quickform\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-hitl>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live HITL dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for Actions.HITL.`, node.nodeId);
}
function dispatchSummarize(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] summarize "${node.nodeId}"\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-summarize>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live Summarize dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for ECS.DeepRag.`, node.nodeId);
}
function dispatchBatchTransform(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        process.stderr.write(`[dispatch] batch-transform "${node.nodeId}"\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-batch-transform>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live Batch Transform dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for ECS.BatchTransform.`, node.nodeId);
}
function dispatchQueue(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const name = node.queueName || node.resourceKey || node.nodeId;
        process.stderr.write(`[dispatch] queue "${node.nodeId}" -> ${name}\n`);
    }
    if (opts.dryRun) {
        return { outputJson: renderFixture(node.fixture), raw: '<dry-run-queue>' };
    }
    throw new DispatchError(`node "${node.nodeId}": live Queue dispatch is not supported by flow-run yet; ` +
        `use --dry-run or run through the Flow/Studio Web debug path for ${node.serviceType}.`, node.nodeId);
}
function renderFixture(fixture) {
    if (fixture === undefined)
        return '{}';
    if (typeof fixture === 'string') {
        const trimmed = fixture.trim();
        if (trimmed.startsWith('{') || trimmed.startsWith('['))
            return fixture;
        return JSON.stringify(fixture);
    }
    return JSON.stringify(fixture);
}
// ─── Script dispatch ────────────────────────────────────────────────────────
//
// `core.action.script` nodes invoked via `await executeNode("name", ...)`
// (NOT inlined as __script_<id> sync helpers) are evaluated by the host so
// their output lands in history. This is the replay-safe path for Math.random,
// Date.now, and other non-deterministic JS calls — the script runs once per
// flow execution and subsequent replays read the recorded value.
//
// The body is v1's `=js:` runtime form: a function body that may reference
// `$vars.<nodeId>.output.<...>` and returns a value. We bind `$vars` to a
// snapshot built from prior history events (each `node:` event's parsed
// output becomes `$vars[nodeId].output`).
function dispatchScript(node, inputJson, opts) {
    if (opts.verbose || opts.dryRun) {
        const preview = node.scriptBody.replace(/\s+/g, ' ').slice(0, 80);
        process.stderr.write(`[dispatch] script "${node.nodeId}" → ${preview}${node.scriptBody.length > 80 ? '...' : ''}\n`);
    }
    if (opts.dryRun) {
        // In dry-run mode, attempt to evaluate the script with whatever $vars
        // context the prior history events produced (other dry-run mocks). If
        // the script throws (e.g. references undefined $vars fields), fall back
        // to '{}'. We DON'T propagate the throw because dry-run is a structural
        // check, not a correctness check.
        try {
            const vars = buildVarsContext(opts.history ?? []);
            const fn = new Function('$vars', node.scriptBody);
            const result = fn(vars);
            const out = result === undefined ? '{}' : JSON.stringify(result);
            return { outputJson: out, raw: '<dry-run-script>' };
        }
        catch {
            return { outputJson: '{}', raw: '<dry-run-script-threw>' };
        }
    }
    const vars = buildVarsContext(opts.history ?? []);
    let result;
    try {
        // Body is a function body: `return ...;` or sequenced statements ending
        // in a return. Expose $vars as a parameter; nothing else is in scope.
        const fn = new Function('$vars', node.scriptBody);
        result = fn(vars);
    }
    catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        throw new DispatchError(`node "${node.nodeId}": script evaluation threw: ${msg}`, node.nodeId, undefined, { script: node.scriptBody.slice(0, 200) });
    }
    if (result === undefined) {
        return { outputJson: 'null', raw: '<undefined>' };
    }
    let outputJson;
    try {
        outputJson = JSON.stringify(result);
    }
    catch (e) {
        throw new DispatchError(`node "${node.nodeId}": script returned a non-JSON-serializable value (${typeof result})`, node.nodeId);
    }
    return { outputJson, raw: outputJson };
}
/**
 * Build the `$vars` context a script body sees. Mirrors v1's runtime: each
 * prior node's output is reachable as `$vars.<nodeId>.output`. Outputs are
 * already-parsed JSON values from history.
 */
function buildVarsContext(history) {
    const ctx = {};
    for (const ev of history) {
        if (ev.kind === 'node') {
            let parsed;
            try {
                parsed = JSON.parse(ev.output);
            }
            catch {
                parsed = ev.output;
            }
            ctx[ev.name] = { output: parsed };
        }
    }
    return ctx;
}
//# sourceMappingURL=dispatcher.js.map