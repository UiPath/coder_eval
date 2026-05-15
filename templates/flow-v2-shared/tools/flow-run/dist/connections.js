"use strict";
// Fetches the user's connection list via `uip is connections list` and
// returns it normalized for binding verification. Called once per run.
Object.defineProperty(exports, "__esModule", { value: true });
exports.ConnectionsFetchError = void 0;
exports.fetchConnections = fetchConnections;
const child_process_1 = require("child_process");
class ConnectionsFetchError extends Error {
    constructor(message, stderr) {
        super(message);
        this.stderr = stderr;
        this.name = 'ConnectionsFetchError';
    }
}
exports.ConnectionsFetchError = ConnectionsFetchError;
function fetchConnections(opts = {}) {
    const args = ['is', 'connections', 'list', '--output', 'json'];
    if (opts.tenant)
        args.push('-t', opts.tenant);
    const proc = (0, child_process_1.spawnSync)('uip', args, { encoding: 'utf8' });
    if (proc.error) {
        throw new ConnectionsFetchError(`failed to spawn 'uip': ${proc.error.message}`);
    }
    if (proc.status !== 0) {
        throw new ConnectionsFetchError(`'uip is connections list' exited ${proc.status}`, proc.stderr || proc.stdout);
    }
    let parsed;
    try {
        parsed = JSON.parse(proc.stdout);
    }
    catch {
        throw new ConnectionsFetchError(`'uip is connections list' did not return JSON`, proc.stdout.slice(0, 500));
    }
    // Envelope: { Result: "Success", Code: "ConnectionList", Data: [...] }
    // Some 'uip' versions may return the array directly — handle both.
    if (Array.isArray(parsed))
        return parsed;
    if (typeof parsed === 'object' && parsed !== null) {
        const env = parsed;
        if (env.Result === 'Failure') {
            throw new ConnectionsFetchError(`'uip is connections list' returned Failure: ${env.Message ?? 'unknown'}`);
        }
        if (Array.isArray(env.Data))
            return env.Data;
    }
    throw new ConnectionsFetchError(`unexpected response shape from 'uip is connections list'`);
}
//# sourceMappingURL=connections.js.map