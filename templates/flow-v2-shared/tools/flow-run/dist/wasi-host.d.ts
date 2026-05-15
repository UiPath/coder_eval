import { CompileError } from 'fil-compiler/dist/compiler';
export declare const WASM_TIMEOUT_MS = 5000;
export interface RunResult {
    stdout: string;
    exitCode: number;
}
export declare class FilCompileError extends Error {
    readonly errors: CompileError[];
    constructor(errors: CompileError[]);
}
export declare class WasmTimeoutError extends Error {
    readonly timeoutMs: number;
    constructor(timeoutMs: number);
}
export declare function compileFil(filSource: string): Promise<Uint8Array>;
export interface RunWasmOptions {
    /** Hard cap on WASM execution. Default: WASM_TIMEOUT_MS (5s). */
    timeoutMs?: number;
}
export declare function runWasm(wasmBytes: Uint8Array, stdinText: string, opts?: RunWasmOptions): Promise<RunResult>;
import { HistoryEvent } from './types';
/**
 * Every line/block FIL emits to stdout falls into one of these shapes.
 * A run's stdout is parsed into a sequence of events; the host walks the
 * sequence in lock-step with history to find the first event the host has
 * not yet satisfied (the "pending" event).
 */
export type ProtocolEvent = {
    kind: 'executeNode';
    name: string;
    input: string;
} | {
    kind: 'executeTimer';
    durationMs?: number;
    deadline?: string;
} | {
    kind: 'all';
    n: number;
} | {
    kind: 'race';
    n: number;
} | {
    kind: 'flowCompleted';
    success: boolean;
};
export declare function parseProtocolEvents(stdout: string): ProtocolEvent[];
/**
 * The next protocol event the host hasn't satisfied yet. Returns null when
 * the events match history exactly (or beyond — flowCompleted indicates
 * the run finished).
 */
export interface PendingState {
    /** Index in `events` of the first unmatched event. */
    index: number;
    /** The unmatched event itself. */
    event: ProtocolEvent;
    /** All events from the protocol stream (returned for callers that need the
     *  full picture, e.g. resolving group entries). */
    events: ProtocolEvent[];
    /** True if a flowCompleted appears anywhere in the stream. */
    flowCompleted: boolean;
}
export declare function findPending(stdout: string, history: HistoryEvent[]): PendingState | null;
/**
 * Build the stdin text the FIL replay parser will consume.
 *
 *   node:           → "node: <name>\n  output: <json>\n"
 *   timer:          → "timer: <ISO>\n"
 *   all-marker:     → "all: N\n"
 *   race-marker:    → "race: N winner=K\n"
 *   node-cancelled: → "nodeCancelled:\n"
 *   timer-cancelled:→ "timerCancelled:\n"
 *
 * Order matters — events must match the sequence FIL emits when re-run.
 */
export declare function buildStdin(events: HistoryEvent[]): string;
export type PendingDecision = {
    kind: 'executeNode';
    name: string;
} | {
    kind: 'executeTimer';
    durationMs?: number;
    deadline?: string;
};
export declare function parsePendingDecision(stdout: string): PendingDecision | null;
