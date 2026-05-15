"use strict";
// Worker thread that runs a FIL-compiled WASM module with a fixed stdin
// buffer and posts back the captured stdout/exitCode. The parent thread
// (wasi-host.ts) sets a hard timeout and terminates this worker if it
// runs over — the only way to actually preempt synchronous WASM execution.
Object.defineProperty(exports, "__esModule", { value: true });
const worker_threads_1 = require("worker_threads");
const buffer_1 = require("buffer");
async function run() {
    const { wasmBytes, stdinText } = worker_threads_1.workerData;
    const stdinBuf = buffer_1.Buffer.from(stdinText, 'utf8');
    let memory = null;
    let stdinPos = 0;
    const stdoutChunks = [];
    let capturedExitCode = 0;
    const fd_read = (fd, iovs, iovsLen, nreadPtr) => {
        if (fd !== 0)
            return 8;
        const view = new DataView(memory.buffer);
        let totalRead = 0;
        for (let i = 0; i < iovsLen; i++) {
            const bufPtr = view.getUint32(iovs + i * 8, true);
            const bufLen = view.getUint32(iovs + i * 8 + 4, true);
            const available = stdinBuf.length - stdinPos;
            const toRead = Math.min(bufLen, available);
            if (toRead > 0) {
                const mem = new Uint8Array(memory.buffer);
                stdinBuf.copy(mem, bufPtr, stdinPos, stdinPos + toRead);
                stdinPos += toRead;
                totalRead += toRead;
            }
        }
        view.setUint32(nreadPtr, totalRead, true);
        return 0;
    };
    const fd_write = (fd, iovs, iovsLen, nwrittenPtr) => {
        if (fd !== 1 && fd !== 2)
            return 8;
        const view = new DataView(memory.buffer);
        let totalWritten = 0;
        for (let i = 0; i < iovsLen; i++) {
            const bufPtr = view.getUint32(iovs + i * 8, true);
            const bufLen = view.getUint32(iovs + i * 8 + 4, true);
            if (fd === 1) {
                stdoutChunks.push(buffer_1.Buffer.from(memory.buffer, bufPtr, bufLen));
            }
            totalWritten += bufLen;
        }
        view.setUint32(nwrittenPtr, totalWritten, true);
        return 0;
    };
    const proc_exit = (code) => {
        capturedExitCode = code;
        throw { __wasiExit: true, code };
    };
    const wasmModule = await WebAssembly.compile(wasmBytes);
    const instance = await WebAssembly.instantiate(wasmModule, {
        wasi_snapshot_preview1: { fd_read, fd_write, proc_exit },
        fil: {
            now_millis: () => BigInt(Date.now()),
            get_uuid: () => 0,
            console_log: () => { },
        },
    });
    const exports = instance.exports;
    memory = exports.memory;
    try {
        exports.start();
    }
    catch (e) {
        const err = e;
        if (!err || !err.__wasiExit)
            throw e;
    }
    worker_threads_1.parentPort.postMessage({
        ok: true,
        stdout: buffer_1.Buffer.concat(stdoutChunks).toString('utf8'),
        exitCode: capturedExitCode,
    });
}
run().catch((e) => {
    worker_threads_1.parentPort.postMessage({
        ok: false,
        error: e instanceof Error ? (e.stack ?? e.message) : String(e),
    });
});
//# sourceMappingURL=wasi-worker.js.map