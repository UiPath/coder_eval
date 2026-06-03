import { HistoryEvent, HistoryFile } from './types';
export interface HistoryFileOptions {
    runId?: string;
    flowId?: string;
    flowName?: string;
    startedAt?: string;
    events?: HistoryEvent[];
}
export declare const historyEvent: {
    node(name: string, output: unknown): HistoryEvent;
    triggerFired(name: string, payload: unknown): HistoryEvent;
    timer(deadline: string): HistoryEvent;
    all(n: number): HistoryEvent;
    race(n: number, winner: number): HistoryEvent;
    nodeCancelled(): HistoryEvent;
    timerCancelled(): HistoryEvent;
};
export declare function historyFile(options?: HistoryFileOptions): HistoryFile;
export declare function loadHistory(historyPath: string): HistoryFile | null;
export declare function saveHistory(historyPath: string, history: HistoryFile): void;
export declare function newHistory(flowId: string, flowName: string): HistoryFile;
export declare function appendEvent(history: HistoryFile, event: HistoryEvent): void;
