import { HistoryEvent, HistoryFile } from './types';
export declare function loadHistory(historyPath: string): HistoryFile | null;
export declare function saveHistory(historyPath: string, history: HistoryFile): void;
export declare function newHistory(flowId: string, flowName: string): HistoryFile;
export declare function appendEvent(history: HistoryFile, event: HistoryEvent): void;
