import type {
    ArtifactRef,
    CriterionResult,
    FlowDebugResult,
    ToolCall,
} from "@/lib/runs";
import { StatusPill } from "@/lib/pills";
import { displayedTurns } from "@/lib/turns";
import { Expandable, KindChip, ResultPill, ToolChip } from "./_chips";

export function FlowDebugSection({ flowDebug }: { flowDebug: FlowDebugResult }) {
    return (
        <section className="space-y-2">
            <div className="flex items-center gap-3">
                <h2 className="text-sm font-semibold text-gray-900">
                    Flow debug elements ({flowDebug.elements.length})
                </h2>
                {flowDebug.finalStatus && (
                    <StatusPill status={flowDebug.finalStatus} />
                )}
            </div>
            {flowDebug.elements.length === 0 ? (
                <div className="text-sm text-gray-500">
                    no element executions
                </div>
            ) : (
                <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                <th className="py-2 px-3 font-medium">
                                    Element
                                </th>
                                <th className="py-2 px-3 font-medium">Type</th>
                                <th className="py-2 px-3 font-medium">
                                    Status
                                </th>
                                <th className="py-2 px-3 font-medium">
                                    Output / error
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {flowDebug.elements.map((e) => (
                                <tr
                                    key={e.elementId}
                                    className="border-b border-gray-100 last:border-b-0"
                                >
                                    <td className="py-2 px-3 font-medium text-gray-900 font-mono text-xs">
                                        {e.elementId}
                                    </td>
                                    <td className="py-2 px-3 text-gray-500 text-xs">
                                        {e.elementType ?? "—"}
                                    </td>
                                    <td className="py-2 px-3">
                                        <StatusPill status={e.status} />
                                    </td>
                                    <td className="py-2 px-3 text-xs text-gray-700 font-mono break-all">
                                        {e.errorMessage ? (
                                            <span className="text-red-700">
                                                {e.errorMessage}
                                            </span>
                                        ) : (
                                            (e.outputPreview ?? "—")
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </section>
    );
}

export function CriteriaSection({ criteria }: { criteria: CriterionResult[] }) {
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">
                Success criteria ({criteria.length})
            </h2>
            <div className="space-y-2">
                {criteria.map((c, i) => {
                    const passed = c.score === 1;
                    return (
                        <Expandable
                            key={i}
                            header={
                                <div className="flex items-center gap-3">
                                    <ResultPill passed={passed} />
                                    <span className="text-sm text-gray-900">
                                        {c.description ??
                                            c.criterionType ??
                                            `criterion ${i + 1}`}
                                    </span>
                                    <span className="ml-auto text-xs text-gray-500 tabular-nums">
                                        score {c.score ?? "—"}
                                    </span>
                                </div>
                            }
                        >
                            {c.details && (
                                <pre className="whitespace-pre-wrap text-xs text-gray-800 bg-gray-50 p-3 rounded border border-gray-200 overflow-x-auto font-mono">
                                    {c.details}
                                </pre>
                            )}
                            {c.error && (
                                <pre className="whitespace-pre-wrap text-xs text-red-700 bg-red-50 p-3 rounded border border-red-200 mt-2 overflow-x-auto font-mono">
                                    {c.error}
                                </pre>
                            )}
                        </Expandable>
                    );
                })}
                {criteria.length === 0 && (
                    <div className="text-sm text-gray-500">
                        no criteria recorded
                    </div>
                )}
            </div>
        </section>
    );
}

export function ToolTimelineSection({
    toolCalls,
    finalAssistantText,
}: {
    toolCalls: ToolCall[];
    finalAssistantText: string | null;
}) {
    const toolCount = toolCalls.length;
    const hasFinalReply = finalAssistantText != null;
    // Single source of truth for the visible turn count, shared with
    // grid / trends / detail-page TURNS stat. SDK num_turns is not
    // consulted — a "turn" is one rendered row.
    const headerCount =
        displayedTurns(toolCount, hasFinalReply) ?? toolCount;
    const finalIndex = toolCount + 1;
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">
                Turn timeline ({headerCount})
            </h2>
            <Expandable
                header={
                    <span className="text-sm text-gray-700">
                        {toolCount} tool call{toolCount === 1 ? "" : "s"} in order
                        {hasFinalReply ? " + final reply" : ""}
                    </span>
                }
            >
                <ol className="space-y-1 text-xs font-mono">
                    {toolCalls.map((t) => (
                        <li
                            key={t.index}
                            className="flex items-start gap-2 py-0.5"
                        >
                            <span className="text-gray-400 tabular-nums w-6 text-right shrink-0">
                                {t.index}.
                            </span>
                            <span className="shrink-0">
                                <ToolChip tool={t.tool} />
                            </span>
                            <span className="text-gray-700 truncate">
                                {t.summary}
                            </span>
                        </li>
                    ))}
                    {hasFinalReply && (
                        <li
                            key="final-reply"
                            className="flex items-start gap-2 py-0.5"
                        >
                            <span className="text-gray-400 tabular-nums w-6 text-right shrink-0">
                                {finalIndex}.
                            </span>
                            <span className="shrink-0">
                                <span className="inline-flex items-center rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
                                    Reply
                                </span>
                            </span>
                            <span className="text-gray-700 whitespace-pre-wrap break-words">
                                {finalAssistantText}
                            </span>
                        </li>
                    )}
                </ol>
            </Expandable>
        </section>
    );
}

export function ArtifactsSection({
    runId,
    artifacts,
}: {
    runId: string;
    artifacts: ArtifactRef[];
}) {
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">Artifacts</h2>
            {artifacts.length === 0 && (
                <div className="text-sm text-gray-500">none</div>
            )}
            <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg bg-white">
                {artifacts.map((a) => (
                    <li
                        key={a.relPath}
                        className="flex items-center gap-3 px-3 py-2 text-sm"
                    >
                        <KindChip kind={a.kind} />
                        <a
                            href={`/api/file?run=${encodeURIComponent(
                                runId,
                            )}&path=${encodeURIComponent(a.relPath)}`}
                            className="text-studio-blue hover:underline truncate"
                            download
                        >
                            {a.relPath}
                        </a>
                        <span className="ml-auto text-xs text-gray-500 tabular-nums">
                            {(a.sizeBytes / 1024).toFixed(1)} KB
                        </span>
                    </li>
                ))}
            </ul>
        </section>
    );
}

export function LogTailSection({ log }: { log: string }) {
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">task.log</h2>
            <Expandable
                header={
                    <span className="text-sm text-gray-700">
                        {log.length.toLocaleString()} bytes
                        <span className="text-gray-400"> · click to view</span>
                    </span>
                }
            >
                <pre className="whitespace-pre-wrap text-xs text-gray-800 bg-gray-50 p-3 rounded border border-gray-200 overflow-x-auto max-h-[600px] overflow-y-auto font-mono">
                    {log}
                </pre>
            </Expandable>
        </section>
    );
}
