import type {
    ArtifactRef,
    CriterionResult,
    FlowDebugResult,
    MessageEvent,
    TokenTotals,
    ToolCall,
} from "@/lib/runs";
import { buildThinkingModel } from "@/lib/thinkingSim";
import { StatusPill } from "@/lib/pills";
import { displayedTurns } from "@/lib/turns";
import { Expandable, KindChip, ResultPill, ToolChip } from "./_chips";
import { ThinkingSimulator } from "./thinking-simulator";

// Thresholds for "this is slow" highlighting on the message timeline.
// Generation > 10 s is unusual: turn 1 priming is ~7 s, steady state is ~2-3 s.
// Tool execution > 5 s is the bar Anthropic SDK uses internally as "slow tool".
const SLOW_GEN_MS = 10_000;
const SLOW_TOOL_MS = 5_000;

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

function fmtMs(ms: number | null): string {
    if (ms == null) return "—";
    if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}m`;
    if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)}s`;
    return `${Math.round(ms)}ms`;
}

function fmtTokens(n: number | null): string {
    if (n == null) return "—";
    if (n === 0) return "0";
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 10_000) return `${(n / 1_000).toFixed(0)}k`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
    return n.toString();
}

// Shared grid template for the message-timeline table. The header row uses
// these exact columns, and every message summary row aligns to them.
const MSG_GRID =
    "grid items-center gap-2 px-2 py-1 " +
    "grid-cols-[1.5rem_2.5rem_3.5rem_3.5rem_minmax(0,1fr)_3.5rem_3.5rem_3.5rem]";

function messageKind(blockTypes: MessageEvent["blockTypes"]): string {
    const set = new Set(blockTypes);
    if (set.size === 0) return "EMPTY";
    if (set.size === 1) {
        const only = blockTypes[0];
        return only === "thinking" ? "THINKING" : only === "tool_use" ? "TOOL" : "TEXT";
    }
    return "MIXED";
}

function kindBadgeClass(kind: string): string {
    switch (kind) {
        case "THINKING":
            return "bg-purple-100 text-purple-700 border-purple-200";
        case "TOOL":
            return "bg-blue-100 text-blue-700 border-blue-200";
        case "TEXT":
            return "bg-green-100 text-green-700 border-green-200";
        case "MIXED":
            return "bg-amber-100 text-amber-700 border-amber-200";
        default:
            return "bg-gray-100 text-gray-600 border-gray-200";
    }
}

export function MessageTimelineSection({ messages }: { messages: MessageEvent[] }) {
    if (messages.length === 0) return null;

    // Roll-up stats for the summary strip.
    const totalGenMs = messages.reduce((s, m) => s + (m.generationMs ?? 0), 0);
    const thinkingMs = messages.reduce((s, m) => s + (m.thinkingMs ?? 0), 0);
    const textMs = messages.reduce((s, m) => s + (m.textMs ?? 0), 0);
    const toolGenMs = messages.reduce((s, m) => s + (m.toolGenMs ?? 0), 0);
    const toolExecMs = messages.reduce(
        (s, m) => s + m.toolUses.reduce((a, t) => a + (t.durationMs ?? 0), 0),
        0,
    );
    const slowGen = messages.filter(
        (m) => (m.generationMs ?? 0) >= SLOW_GEN_MS,
    ).length;
    const slowTool = messages.reduce(
        (s, m) =>
            s + m.toolUses.filter((t) => (t.durationMs ?? 0) >= SLOW_TOOL_MS).length,
        0,
    );
    const thinkingShare = totalGenMs > 0 ? thinkingMs / totalGenMs : 0;

    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">
                Message timeline ({messages.length})
            </h2>
            <p className="text-[10px] text-gray-500">
                MIXED = multiple block types · red = slow (gen ≥10s, tool ≥5s)
            </p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs bg-gray-50 border border-gray-200 rounded-lg p-3 tabular-nums">
                <div>
                    <div className="text-gray-500 uppercase tracking-wide text-[10px]">
                        Messages
                    </div>
                    <div className="text-gray-900 font-medium">{messages.length}</div>
                </div>
                <div>
                    <div className="text-gray-500 uppercase tracking-wide text-[10px]">
                        Generation
                    </div>
                    <div className="text-gray-900 font-medium">
                        {fmtMs(totalGenMs)}
                    </div>
                    <div className="mt-1 grid grid-cols-3 gap-2 text-[10px]">
                        <div>
                            <div className="text-gray-500">thinking</div>
                            <div
                                className={
                                    thinkingShare >= 0.4
                                        ? "text-red-700 font-medium tabular-nums"
                                        : "text-gray-800 font-medium tabular-nums"
                                }
                            >
                                {fmtMs(thinkingMs)}
                                {totalGenMs > 0 && (
                                    <span className="text-gray-400">
                                        {" "}
                                        ({Math.round(thinkingShare * 100)}%)
                                    </span>
                                )}
                            </div>
                        </div>
                        <div>
                            <div className="text-gray-500">tool</div>
                            <div className="text-gray-800 font-medium tabular-nums">
                                {fmtMs(toolGenMs)}
                                {totalGenMs > 0 && (
                                    <span className="text-gray-400">
                                        {" "}
                                        ({Math.round((toolGenMs / totalGenMs) * 100)}%)
                                    </span>
                                )}
                            </div>
                        </div>
                        <div>
                            <div className="text-gray-500">text</div>
                            <div className="text-gray-800 font-medium tabular-nums">
                                {fmtMs(textMs)}
                                {totalGenMs > 0 && (
                                    <span className="text-gray-400">
                                        {" "}
                                        ({Math.round((textMs / totalGenMs) * 100)}%)
                                    </span>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
                <div>
                    <div className="text-gray-500 uppercase tracking-wide text-[10px]">
                        Tool exec
                    </div>
                    <div className="text-gray-900 font-medium">
                        {fmtMs(toolExecMs)}
                    </div>
                </div>
                <div>
                    <div className="text-gray-500 uppercase tracking-wide text-[10px]">
                        Slow events
                    </div>
                    <div
                        className={
                            slowGen + slowTool > 0
                                ? "text-red-700 font-medium"
                                : "text-gray-900 font-medium"
                        }
                    >
                        {slowGen} gen · {slowTool} tool
                    </div>
                </div>
            </div>
            <div className="border border-gray-200 rounded-lg overflow-hidden bg-white text-xs font-mono">
                <div
                    className={
                        MSG_GRID +
                        " bg-gray-50 border-b border-gray-200 text-[10px] uppercase tracking-wide text-gray-500 font-sans"
                    }
                >
                    <span aria-hidden="true" />
                    <span className="text-right">#</span>
                    <span className="text-right">Gen</span>
                    <span className="text-right">Exec</span>
                    <span>Content</span>
                    <span className="text-right">Out</span>
                    <span className="text-right">Cache W</span>
                    <span className="text-right">Cache R</span>
                </div>
                <ol>
                    {messages.map((m) => (
                        <MessageRow key={m.index} m={m} />
                    ))}
                </ol>
            </div>
        </section>
    );
}

export function ThinkingCostSection({
    messages,
    tokens,
    recordedCostUsd,
}: {
    messages: MessageEvent[];
    tokens: TokenTotals;
    recordedCostUsd: number | null;
}) {
    const model = buildThinkingModel(messages, tokens, recordedCostUsd);
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">
                Cost simulator
            </h2>
            <p className="text-[10px] text-gray-500">
                Project this run&apos;s cost if it had thought more or less, or
                if tools had returned more or less. Both account for the cache
                cascade — trimming early content shrinks the transcript every
                later call re-reads.
            </p>
            {model ? (
                <ThinkingSimulator model={model} />
            ) : (
                <div className="text-sm text-gray-500 border border-gray-200 rounded-lg p-3 bg-white">
                    Can&apos;t project — no recognized model pricing for this
                    run&apos;s messages.
                </div>
            )}
        </section>
    );
}

function firstLine(s: string | null, cap = 100): string {
    if (!s) return "";
    const line = s.split(/\r?\n/)[0].trim();
    return line.length > cap ? line.slice(0, cap - 1) + "…" : line;
}

function summaryPreview(m: MessageEvent): string {
    // One short line for the collapsed row: prefer tool arg, then text, then
    // thinking. Truncated so wide tasks still fit on a single row.
    if (m.toolUses.length > 0) {
        const t = m.toolUses[0];
        const head = firstLine(t.argText ?? t.description ?? t.summary, 90);
        return m.toolUses.length > 1
            ? `${head}   (+${m.toolUses.length - 1} more)`
            : head;
    }
    if (m.text) return firstLine(m.text, 100);
    if (m.thinkingText) return firstLine(m.thinkingText, 100);
    return "";
}

// One sub-block row inside an expanded MessageRow. Aligns to the same MSG_GRID
// columns as the parent so Gen / Exec / Content / Out line up. The Out cell
// carries the block's own recorded per-emission output_tokens; Cache W / Cache R
// stay blank — they're input-side and not attributable per block.
function SubRow({
    kind,
    genMs,
    execMs,
    isError,
    toolName,
    outputTokens,
    children,
}: {
    kind: "thinking" | "tool" | "text";
    genMs: number | null;
    execMs: number | null;
    isError: boolean;
    toolName?: string;
    // Per-block output tokens. For thinking it's the thinking emission's real
    // output_tokens; for text/tool it's an approximation split from the
    // remaining output by gen-time weight. Cache W/R don't apply per-block.
    outputTokens: number | null;
    children: React.ReactNode;
}) {
    const slowExec = (execMs ?? 0) >= SLOW_TOOL_MS;
    const label =
        kind === "thinking" ? (
            <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium shrink-0 bg-purple-100 text-purple-700 border-purple-200">
                thinking
            </span>
        ) : kind === "text" ? (
            <span className="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium shrink-0 bg-green-100 text-green-700 border-green-200">
                text
            </span>
        ) : (
            <span className="shrink-0 flex items-center gap-1">
                <ToolChip tool={toolName ?? "unknown"} />
                {isError && (
                    <span className="inline-flex items-center rounded border border-red-200 bg-red-50 text-red-700 px-1 py-0.5 text-[10px]">
                        error
                    </span>
                )}
            </span>
        );
    return (
        <div className={MSG_GRID + " items-start"}>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span className="tabular-nums text-right text-gray-500">
                {fmtMs(genMs)}
            </span>
            <span
                className={
                    "tabular-nums text-right " +
                    (slowExec ? "text-red-700 font-medium" : "text-gray-500")
                }
            >
                {execMs != null ? fmtMs(execMs) : "—"}
            </span>
            <div className="min-w-0 space-y-1">
                <div>{label}</div>
                {children}
            </div>
            <span className="tabular-nums text-right text-gray-500">
                {fmtTokens(outputTokens)}
            </span>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
        </div>
    );
}

function MessageRow({ m }: { m: MessageEvent }) {
    const kind = messageKind(m.blockTypes);
    const slowGen = (m.generationMs ?? 0) >= SLOW_GEN_MS;
    const slowTool = m.toolUses.some((t) => (t.durationMs ?? 0) >= SLOW_TOOL_MS);
    const hasErrorTool = m.toolUses.some((t) => t.isError);
    const preview = summaryPreview(m);
    // Sum tool exec time for this message — matches the rollup strip.
    const execMs = m.toolUses.reduce((a, t) => a + (t.durationMs ?? 0), 0);
    const hasExec = m.toolUses.some((t) => t.durationMs != null);
    // Render full body only when something more than the summary exists.
    const hasBody =
        m.toolUses.length > 0 ||
        (m.thinkingText != null && m.thinkingText.length > 0) ||
        (m.text != null && m.text.length > preview.length);
    const rowTint = hasErrorTool
        ? "border-red-100 bg-red-50/30"
        : "border-gray-100";
    return (
        <li className={"border-b last:border-b-0 " + rowTint}>
            <details className="group">
                <summary
                    className={
                        MSG_GRID +
                        " cursor-pointer list-none [&::-webkit-details-marker]:hidden hover:bg-gray-50"
                    }
                >
                    <span
                        aria-hidden="true"
                        className="inline-block w-3 text-gray-400 transition-transform group-open:rotate-90"
                    >
                        ▶
                    </span>
                    <span className="text-gray-500 tabular-nums text-right">
                        {m.index}
                    </span>
                    <span
                        className={
                            "tabular-nums text-right " +
                            (slowGen
                                ? "text-red-700 font-medium"
                                : "text-gray-600")
                        }
                    >
                        {fmtMs(m.generationMs)}
                    </span>
                    <span
                        className={
                            "tabular-nums text-right " +
                            (slowTool
                                ? "text-red-700 font-medium"
                                : "text-gray-600")
                        }
                    >
                        {hasExec ? fmtMs(execMs) : "—"}
                    </span>
                    <span className="flex items-center gap-2 min-w-0">
                        <span
                            className={
                                "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium shrink-0 " +
                                kindBadgeClass(kind)
                            }
                        >
                            {kind}
                        </span>
                        {m.toolUses[0] && (
                            <span className="shrink-0">
                                <ToolChip tool={m.toolUses[0].toolName} />
                            </span>
                        )}
                        {hasErrorTool && (
                            <span className="inline-flex items-center rounded border border-red-200 bg-red-50 text-red-700 px-1 py-0.5 text-[10px] shrink-0">
                                error
                            </span>
                        )}
                        <span className="text-gray-700 truncate min-w-0">
                            {preview}
                        </span>
                    </span>
                    <span className="tabular-nums text-right text-gray-600">
                        {fmtTokens(m.outputTokens)}
                    </span>
                    <span className="tabular-nums text-right text-gray-600">
                        {fmtTokens(m.cacheWriteTokens)}
                    </span>
                    <span className="tabular-nums text-right text-gray-600">
                        {fmtTokens(m.cacheReadTokens)}
                    </span>
                </summary>
                {hasBody && (
                    <div className="border-t border-gray-100 bg-gray-50/40 divide-y divide-gray-100">
                        {m.thinkingText && (
                            <SubRow
                                kind="thinking"
                                genMs={m.thinkingMs}
                                execMs={null}
                                isError={false}
                                outputTokens={m.thinkingOutputTokens}
                            >
                                <div className="text-gray-600 whitespace-pre-wrap break-words">
                                    {m.thinkingText}
                                </div>
                            </SubRow>
                        )}
                        {m.toolUses.map((t, i) => (
                            <SubRow
                                key={`${m.index}-tool-${i}`}
                                kind="tool"
                                genMs={t.genMs}
                                execMs={t.durationMs}
                                isError={t.isError}
                                toolName={t.toolName}
                                outputTokens={t.outputTokens}
                            >
                                {t.description && (
                                    <div className="text-gray-500 italic mb-1">
                                        {t.description}
                                    </div>
                                )}
                                {t.argText && (
                                    <div className="text-gray-800 whitespace-pre-wrap break-all bg-white border border-gray-200 rounded px-2 py-1">
                                        {t.argText}
                                    </div>
                                )}
                                {t.resultPreview && (
                                    <div
                                        className={
                                            "mt-1 text-[11px] whitespace-pre-wrap break-words border rounded px-2 py-1 " +
                                            (t.isError
                                                ? "border-red-200 bg-red-50/40 text-red-800"
                                                : "border-gray-100 bg-white text-gray-600")
                                        }
                                    >
                                        <span className="text-[10px] uppercase tracking-wide text-gray-400 mr-1">
                                            result
                                        </span>
                                        {t.resultPreview}
                                    </div>
                                )}
                            </SubRow>
                        ))}
                        {m.text && (
                            <SubRow
                                kind="text"
                                genMs={m.textMs}
                                execMs={null}
                                isError={false}
                                outputTokens={m.textOutputTokens}
                            >
                                <div className="text-gray-700 whitespace-pre-wrap break-words">
                                    {m.text}
                                </div>
                            </SubRow>
                        )}
                    </div>
                )}
            </details>
        </li>
    );
}

// Cap the always-visible list so a task that preserved a large tree (e.g. a
// cloned fixture repo) doesn't render hundreds of rows. Overflow goes behind a
// "show N more" disclosure. sortArtifacts already floats deliverables to top,
// so the capped head holds the rows that matter.
const ARTIFACT_CAP = 50;

function ArtifactList({
    runId,
    items,
}: {
    runId: string;
    items: ArtifactRef[];
}) {
    return (
        <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg bg-white">
            {items.map((a) => (
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
    );
}

export function ArtifactsSection({
    runId,
    artifacts,
}: {
    runId: string;
    artifacts: ArtifactRef[];
}) {
    const head = artifacts.slice(0, ARTIFACT_CAP);
    const rest = artifacts.slice(ARTIFACT_CAP);
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">
                Artifacts ({artifacts.length})
            </h2>
            {artifacts.length === 0 ? (
                <div className="text-sm text-gray-500">none</div>
            ) : (
                <ArtifactList runId={runId} items={head} />
            )}
            {rest.length > 0 && (
                <Expandable
                    header={
                        <span className="text-sm text-gray-700">
                            Show {rest.length} more
                        </span>
                    }
                >
                    <ArtifactList runId={runId} items={rest} />
                </Expandable>
            )}
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
