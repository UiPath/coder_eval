import { HistoryEvent, ResolvedAgentNode, ResolvedApiWorkflowNode, ResolvedConnectorNode, ResolvedHitlNode, ResolvedHttpNode, ResolvedInlineAgentNode, ResolvedMockNode, ResolvedNode, ResolvedRpaWorkflowNode, ResolvedScriptNode, ResolvedSummarizeNode } from './types';
export interface DispatchResult {
    outputJson: string;
    raw: string;
}
export declare class DispatchError extends Error {
    readonly nodeId: string;
    readonly stderr?: string | undefined;
    readonly envelope?: unknown | undefined;
    constructor(message: string, nodeId: string, stderr?: string | undefined, envelope?: unknown | undefined);
}
export interface DispatchOptions {
    dryRun: boolean;
    verbose: boolean;
    tenant?: string;
    /** Prior history events — used to build $vars context for script nodes. */
    history?: HistoryEvent[];
}
export declare function dispatch(node: ResolvedNode, inputJson: string, opts: DispatchOptions): Promise<DispatchResult>;
export declare function dispatchConnector(node: ResolvedConnectorNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchHttp(node: ResolvedHttpNode, inputJson: string, opts: DispatchOptions): Promise<DispatchResult>;
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
export declare function mergeHttpInputs(rawInputs: Record<string, unknown>, filInput: Record<string, unknown>): Record<string, unknown>;
export declare function dispatchMock(node: ResolvedMockNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchAgent(node: ResolvedAgentNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchApiWorkflow(node: ResolvedApiWorkflowNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchRpaWorkflow(node: ResolvedRpaWorkflowNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchInlineAgent(node: ResolvedInlineAgentNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchHitl(node: ResolvedHitlNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchSummarize(node: ResolvedSummarizeNode, inputJson: string, opts: DispatchOptions): DispatchResult;
export declare function dispatchScript(node: ResolvedScriptNode, inputJson: string, opts: DispatchOptions): DispatchResult;
