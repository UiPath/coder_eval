import { DecisionRecord, HistoryFile, ResolvedNode, RunOptions } from './types';
import { LoadedProject } from './loader';
export interface RunReport {
    success: boolean;
    steps: number;
    history: HistoryFile;
    decisions: DecisionRecord[];
    error?: string;
}
export declare function runFlow(project: LoadedProject, resolved: ResolvedNode[], opts: RunOptions): Promise<RunReport>;
