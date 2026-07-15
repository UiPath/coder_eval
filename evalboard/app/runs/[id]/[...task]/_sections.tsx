"use client";

import { type ReactNode, useState } from "react";
import type {
    ArtifactRef,
    ConversationTurn,
    CriterionResult,
    FlowDebugResult,
    MessageEvent,
    MessageToolUse,
    SubAgentTotals,
    TokenTotals,
    ToolCall,
} from "@/lib/runs";
import {
    type PerMessageImpact,
    buildThinkingModel,
    projectPerMessage,
} from "@/lib/thinkingSim";
import { fmtCompact, fmtUsd } from "@/lib/format";
import { tokenBucketUsd, type TokenKind } from "@/lib/pricing";
import {
    type ColHelp,
    ColHelpIcon,
    TOKEN_COLUMN_HELP,
} from "@/app/_components/col-help";
import { type Unit, UnitToggle } from "@/app/_components/unit-toggle";
import { TableScroll } from "@/app/_components/scroll-table";
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
                <TableScroll>
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
                </TableScroll>
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
    "grid-cols-[1.5rem_2.5rem_3.5rem_3.5rem_minmax(0,1fr)_3.5rem_3.5rem_3.5rem_3.5rem_4.5rem]";

// Per-message Cost help (grid-specific: this is a rate-derived per-call figure,
// not the SDK's cumulative per-turn cost). Token-column help is shared via
// TOKEN_COLUMN_HELP so the timeline and the run grid stay consistent.
const MESSAGE_COST_HELP: ColHelp = {
    title: "Per-message cost",
    body: "This message's recorded tokens priced at list rates — the cost of this single API call. The SDK reports only a cumulative per-turn figure, so these need not sum exactly to the task's total cost (the authoritative SDK number) shown above. Blank when the model is unpriced or no per-message tokens were recorded.",
};

// A right-aligned message-timeline header cell with an ⓘ help bubble. Label sits
// at the right edge with the icon to its left (flex-row-reverse), matching the
// run-grid headers.
function MsgHeadHelp({ label, help }: { label: string; help: ColHelp }) {
    return (
        <span className="inline-flex items-center justify-end gap-1 flex-row-reverse">
            {label}
            <ColHelpIcon help={help} align="right" />
        </span>
    );
}

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

export function MessageTimelineSection({
    messages,
    subAgentUsageByToolId = {},
    impactByIndex,
}: {
    messages: MessageEvent[];
    // Per-Agent-call sub-agent token breakdown (input/output/cache-create/
    // cache-read), keyed by the spawning tool_use_id. Surfaced on each Agent
    // call's result row.
    subAgentUsageByToolId?: Record<string, SubAgentTotals>;
    // Projected per-message token delta from the cost simulator's levers, keyed
    // by message index. Renders an inline Δ badge on each affected row. Empty
    // (no badges) when levers sit at as-run, or undefined when no simulator.
    impactByIndex?: Map<number, PerMessageImpact>;
}) {
    // Token columns can be shown as counts or as their estimated USD value.
    const [unit, setUnit] = useState<Unit>("tokens");

    if (messages.length === 0) return null;

    // Group sub-agent emissions under the Agent tool call that spawned them.
    // The conversation is a tree: sub-agent (Task tool) calls bubble up into the
    // flat stream tagged with the spawning tool_use_id (parent_tool_use_id).
    // We key children by that id and render only the main thread at top level;
    // each sub-agent's invocations nest, expandable, under its Agent call.
    // Legacy runs (parentToolUseId undefined) have no string parents → the map
    // stays empty and every message renders flat, exactly as before.
    const childrenByParent = new Map<string, MessageEvent[]>();
    for (const m of messages) {
        const p = m.parentToolUseId;
        if (typeof p === "string") {
            const arr = childrenByParent.get(p) ?? [];
            arr.push(m);
            childrenByParent.set(p, arr);
        }
    }
    // == null matches both null (main thread) and undefined (legacy, no branch
    // info) → both render at the top level. Sub-agent emissions render nested
    // inside the message that spawned them (see MessageRow).
    const topLevelMessages = messages.filter((m) => m.parentToolUseId == null);
    // Count real generations only — the synthetic reconciliation row is a meta
    // entry, not a message.
    const messageCount = messages.filter((m) => m.role === "assistant").length;

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
            <div className="flex items-center justify-between gap-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    Message timeline ({messageCount})
                </h2>
                <UnitToggle value={unit} onChange={setUnit} />
            </div>
            <p className="text-[10px] text-gray-500">
                MIXED = multiple block types · red = slow (gen ≥10s, tool ≥5s)
            </p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs bg-gray-50 border border-gray-200 rounded-lg p-3 tabular-nums">
                <div>
                    <div className="text-gray-500 uppercase tracking-wide text-[10px]">
                        Messages
                    </div>
                    <div className="text-gray-900 font-medium">{messageCount}</div>
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
            <TableScroll>
                {/* min-width keeps the fixed grid from crushing on a phone — it
                    scrolls horizontally as a unit instead, with the content
                    column staying readable. */}
                <div className="min-w-[46rem] text-xs font-mono">
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
                    <span className="text-right">
                        <MsgHeadHelp
                            label="In"
                            help={TOKEN_COLUMN_HELP.input}
                        />
                    </span>
                    <span className="text-right">
                        <MsgHeadHelp
                            label="Cache R"
                            help={TOKEN_COLUMN_HELP.cr}
                        />
                    </span>
                    <span className="text-right">
                        <MsgHeadHelp
                            label="Cache W"
                            help={TOKEN_COLUMN_HELP.cw}
                        />
                    </span>
                    <span className="text-right">
                        <MsgHeadHelp
                            label="Out"
                            help={TOKEN_COLUMN_HELP.output}
                        />
                    </span>
                    <span className="text-right">
                        <MsgHeadHelp label="Cost" help={MESSAGE_COST_HELP} />
                    </span>
                </div>
                <ol>
                    {topLevelMessages.map((m) =>
                        m.role === "reconciliation" ? (
                            <ReconciliationRow key={m.index} m={m} unit={unit} />
                        ) : impactByIndex?.get(m.index)?.eliminated ? (
                            // Every tool in this call is skipped → the whole API
                            // call never happens. The simulator already removed its
                            // full footprint (output + own prompt) from the totals,
                            // so the row collapses to a slim "removed" placeholder
                            // rather than a live token line — keeping the index
                            // sequence legible without pretending the call ran.
                            <RemovedRow
                                key={m.index}
                                m={m}
                                impact={impactByIndex.get(m.index)!}
                            />
                        ) : (
                            <MessageRow
                                key={m.index}
                                m={m}
                                unit={unit}
                                childrenByParent={childrenByParent}
                                subAgentUsageByToolId={subAgentUsageByToolId}
                                impactByIndex={impactByIndex}
                            />
                        ),
                    )}
                </ol>
                </div>
            </TableScroll>
        </section>
    );
}

// Owns the cost-simulator lever state and renders BOTH the message timeline and
// the simulator beneath it. Lifting the levers here lets the timeline show each
// message's projected token Δ inline (the "message view" of the diff) driven by
// the very same levers the simulator exposes.
export function CostExplorerSection({
    messages,
    subAgentUsageByToolId = {},
    tokens,
    recordedCostUsd,
}: {
    messages: MessageEvent[];
    subAgentUsageByToolId?: Record<string, SubAgentTotals>;
    tokens: TokenTotals;
    recordedCostUsd: number | null;
}) {
    const [scale, setScale] = useState(1);
    const [toolScale, setToolScale] = useState(1);
    const [skippedTools, setSkippedTools] = useState<Set<string>>(new Set());
    const toggleTool = (toolName: string) => {
        setSkippedTools((prev) => {
            const next = new Set(prev);
            if (next.has(toolName)) next.delete(toolName);
            else next.add(toolName);
            return next;
        });
    };

    // No model → the run can't be projected (unpriced model, or it predates
    // per-call branch capture so the cache cascade can't be modeled as a tree).
    // The timeline still renders; the simulator + inline deltas are hidden.
    const model = buildThinkingModel(messages, tokens, recordedCostUsd);
    const leversActive =
        model != null &&
        tokens.total > 0 &&
        (scale !== 1 || toolScale !== 1 || skippedTools.size > 0);

    // Per-message projected token deltas, keyed by message index — drives the
    // inline Δ badge on each timeline row. Empty (no badges) at as-run levers.
    const impactByIndex = new Map<number, PerMessageImpact>();
    if (model && leversActive) {
        for (const imp of projectPerMessage(model, scale, toolScale, skippedTools)) {
            impactByIndex.set(imp.messageIndex, imp);
        }
    }

    return (
        <>
            <MessageTimelineSection
                messages={messages}
                subAgentUsageByToolId={subAgentUsageByToolId}
                impactByIndex={impactByIndex}
            />
            {model && tokens.total > 0 && (
                <section className="space-y-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Cost simulator
                    </h2>
                    <p className="text-[10px] text-gray-500">
                        Project this run&apos;s cost if it had thought more or
                        less, or if tools had returned more or less. Both account
                        for the cache cascade — trimming early content shrinks the
                        transcript every later call re-reads. Drag a lever to see
                        each message&apos;s projected Δ inline in the timeline
                        above.
                    </p>
                    <ThinkingSimulator
                        model={model}
                        scale={scale}
                        setScale={setScale}
                        toolScale={toolScale}
                        setToolScale={setToolScale}
                        skippedTools={skippedTools}
                        toggleTool={toggleTool}
                    />
                </section>
            )}
        </>
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

// The RETURN VALUE preview of an Agent (sub-agent) tool call. The sub-agent's
// token buckets are now shown per-call on each SubAgentCallTokensRow inside the
// expansion (where the cost is actually incurred — notably the first call's
// delegation-prompt cache-write), so this row carries only the return preview;
// its token columns stay blank to avoid double-counting the per-call rows.
function SubAgentResultRow({
    preview,
    isError,
}: {
    preview: string | null;
    isError: boolean;
}) {
    return (
        <div className={MSG_GRID + " items-start py-1 bg-studio-blue/[0.03]"}>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <div className="min-w-0">
                <div
                    className={
                        "text-[11px] whitespace-pre-wrap break-words border rounded px-2 py-1 " +
                        (isError
                            ? "border-red-200 bg-red-50/40 text-red-800"
                            : "border-gray-100 bg-white text-gray-600")
                    }
                >
                    <span className="text-[10px] uppercase tracking-wide text-gray-400 mr-1">
                        result
                    </span>
                    {preview}
                </div>
            </div>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
        </div>
    );
}

// One API call's OWN token buckets + cost — the complete per-call token line,
// rendered as the lead row inside that call's expansion for BOTH main-thread
// messages and nested sub-agent calls, so every call reads the same way. This is
// the single home for a call's In / Cache R / Cache W / Out / Cost (the
// expandable summary row above no longer repeats the sum). The simulator's
// projected Δ for each bucket shows here too. The block rows below still split
// the call's Out across thinking/text/tool — a breakdown of the Out shown here.
function CallTokensRow({
    m,
    unit,
    impact,
}: {
    m: MessageEvent;
    unit: Unit;
    impact?: PerMessageImpact;
}) {
    const fmtTok = (tokens: number | null, kind: TokenKind) =>
        unit === "usd"
            ? fmtUsd(tokenBucketUsd(m.model, tokens, kind))
            : fmtTokens(tokens);
    return (
        <div className={MSG_GRID + " items-start py-0.5 bg-studio-blue/[0.04]"}>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <div className="min-w-0 text-[10px] uppercase tracking-wide text-studio-blue/70">
                call tokens
            </div>
            <TokenDeltaCell
                value={fmtTok(m.inputTokens, "input")}
                delta={impact?.dInput}
                kind="input"
                unit={unit}
                model={m.model}
            />
            <TokenDeltaCell
                value={fmtTok(m.cacheReadTokens, "cacheRead")}
                delta={impact?.dCacheRead}
                kind="cacheRead"
                unit={unit}
                model={m.model}
            />
            <TokenDeltaCell
                value={fmtTok(m.cacheWriteTokens, "cacheWrite")}
                delta={impact?.dCacheWrite}
                kind="cacheWrite"
                unit={unit}
                model={m.model}
            />
            <TokenDeltaCell
                value={fmtTok(m.outputTokens, "output")}
                delta={impact?.dOutput}
                kind="output"
                unit={unit}
                model={m.model}
            />
            <span className="tabular-nums text-right text-gray-600">
                {m.costUsd != null ? fmtUsd(m.costUsd) : "—"}
            </span>
        </div>
    );
}

// One sub-block row inside an expanded MessageRow. Aligns to the same MSG_GRID
// columns as the parent so Gen / Exec / Content / Out line up. The Out cell
// carries the block's own recorded per-emission output_tokens; Cache R / Cache W
// / Cost stay blank — they're per-call (input-side / whole-message) and not
// attributable to an individual block.
function SubRow({
    kind,
    genMs,
    execMs,
    isError,
    toolName,
    outputTokens,
    fmtOut,
    expandable = false,
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
    // Formats the Out cell — token count or estimated USD, matching the row's
    // unit toggle. Passed down so the sub-row aligns with the parent.
    fmtOut: (tokens: number | null) => string;
    // When true, this row is the summary of a nested <details> — a tool call
    // that wraps the child tool rows it spawned. Renders a chevron in the first
    // column that rotates open via the .group-chevron CSS rule.
    expandable?: boolean;
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
            {expandable ? (
                <span className="group-chevron inline-block w-3 text-studio-blue/70 transition-transform">
                    ▶
                </span>
            ) : (
                <span aria-hidden="true" />
            )}
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
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span className="tabular-nums text-right text-gray-500">
                {fmtOut(outputTokens)}
            </span>
            <span aria-hidden="true" />
        </div>
    );
}

// The inner rows of a message — thinking, each tool call, and trailing text —
// rendered without a message-level header or expand toggle. Used both inside an
// expanded MessageRow and, FLATTENED, for a sub-agent's bubbled emissions: a
// child message whose tools have no children of their own shows its tool rows
// directly (no per-message disclosure), while a tool that DID spawn children
// stays an expandable group. Recurses via itself, so only child-bearing tool
// calls ever carry a chevron.
function MessageBody({
    m,
    unit,
    childrenByParent,
    subAgentUsageByToolId = {},
    impactByIndex,
}: {
    m: MessageEvent;
    unit: Unit;
    childrenByParent: Map<string, MessageEvent[]>;
    subAgentUsageByToolId?: Record<string, SubAgentTotals>;
    // Projected per-message Δ keyed by message index — threaded down so each
    // nested sub-agent call's CallTokensRow shows its own bucket deltas too.
    impactByIndex?: Map<number, PerMessageImpact>;
}) {
    const fmtTok = (tokens: number | null, kind: TokenKind) =>
        unit === "usd"
            ? fmtUsd(tokenBucketUsd(m.model, tokens, kind))
            : fmtTokens(tokens);
    const fmtOut = (tokens: number | null) => fmtTok(tokens, "output");
    const thinkingRow = m.thinkingText ? (
        <SubRow
            key="thinking"
            kind="thinking"
            genMs={m.thinkingMs}
            execMs={null}
            isError={false}
            outputTokens={m.thinkingOutputTokens}
            fmtOut={fmtOut}
        >
            <div className="text-gray-600 whitespace-pre-wrap break-words">
                {m.thinkingText}
            </div>
        </SubRow>
    ) : null;
    const textRow = m.text ? (
        <SubRow
            key="text"
            kind="text"
            genMs={m.textMs}
            execMs={null}
            isError={false}
            outputTokens={m.textOutputTokens}
            fmtOut={fmtOut}
        >
            <div className="text-gray-700 whitespace-pre-wrap break-words">
                {m.text}
            </div>
        </SubRow>
    ) : null;
    const renderTool = (t: MessageToolUse, i: number) => {
                // Child tool calls this tool spawned (matched by
                // parent_tool_use_id). Any tool with children becomes an
                // expandable group; in practice it's the Agent (sub-agent)
                // tool, but nothing here hardcodes that.
                const kids = t.toolUseId
                    ? childrenByParent.get(t.toolUseId)
                    : undefined;
                const hasKids = !!kids && kids.length > 0;
                // Complete per-sub-agent token breakdown for this Agent call
                // (input/output/cache-create/cache-read), from the
                // tool-result's tool_use_result.usage. When present, this is a
                // sub-agent (Agent-tool) call and its result renders as a grid
                // row with the Cache R / Cache W / Out columns filled.
                const subUsage = t.toolUseId
                    ? subAgentUsageByToolId[t.toolUseId]
                    : undefined;
                // Inline result for ordinary tools (Bash/Write/…): a block
                // inside the call's Content cell, no token columns of its own.
                const inlineResultBlock = t.resultPreview ? (
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
                ) : null;
                // Tool chip + args. Reused whether the row is a plain SubRow or
                // the summary of an expandable group.
                const callBody = (
                    <>
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
                    </>
                );
                // The call's result, rendered below it (and below any expanded
                // child rows). Sub-agent → token-bearing grid row; ordinary tool
                // with bubbled children → the inline block.
                const resultBelow = subUsage ? (
                    <SubAgentResultRow
                        preview={t.resultPreview}
                        isError={t.isError}
                    />
                ) : (
                    hasKids &&
                    inlineResultBlock && (
                        <div className="border-l-2 border-studio-blue/40 pl-2 py-1">
                            {inlineResultBlock}
                        </div>
                    )
                );
                return (
                    <div
                        key={`${m.index}-tool-${i}`}
                        className="divide-y divide-gray-100"
                    >
                        {hasKids ? (
                            // Tool call that spawned children: collapsed by
                            // default. The call row is the <summary>; expanding
                            // it reveals the child tool rows, rendered FLAT (no
                            // per-message disclosure) so a childless sub-tool
                            // never gets its own toggle.
                            <details className="group/grouped">
                                <summary className="list-none [&::-webkit-details-marker]:hidden cursor-pointer hover:bg-gray-50">
                                    <SubRow
                                        kind="tool"
                                        genMs={t.genMs}
                                        execMs={t.durationMs}
                                        isError={t.isError}
                                        toolName={t.toolName}
                                        outputTokens={t.outputTokens}
                                        fmtOut={fmtOut}
                                        expandable
                                    >
                                        {callBody}
                                    </SubRow>
                                </summary>
                                <ol className="border-l-2 border-studio-blue/40 bg-studio-blue/[0.03] divide-y divide-gray-100">
                                    {kids.map((c) => (
                                        <li key={c.index}>
                                            <CallTokensRow
                                                m={c}
                                                unit={unit}
                                                impact={impactByIndex?.get(
                                                    c.index,
                                                )}
                                            />
                                            <MessageBody
                                                m={c}
                                                unit={unit}
                                                childrenByParent={
                                                    childrenByParent
                                                }
                                                subAgentUsageByToolId={
                                                    subAgentUsageByToolId
                                                }
                                                impactByIndex={impactByIndex}
                                            />
                                        </li>
                                    ))}
                                </ol>
                            </details>
                        ) : (
                            <SubRow
                                kind="tool"
                                genMs={t.genMs}
                                execMs={t.durationMs}
                                isError={t.isError}
                                toolName={t.toolName}
                                outputTokens={t.outputTokens}
                                fmtOut={fmtOut}
                            >
                                {callBody}
                                {/* Ordinary tool with no children: result is
                                    inline in the Content cell. Sub-agent results
                                    render as a grid row below instead. */}
                                {!subUsage && inlineResultBlock}
                            </SubRow>
                        )}
                        {resultBelow}
                    </div>
                );
    };
    // Render blocks in their actual emission order (content_blocks sequence),
    // interleaving text and tool calls instead of forcing thinking→tools→text.
    // `blockTypes` preserves order for both agents; thinking/text are aggregated
    // per message, so each renders once at its first occurrence while tool_use
    // blocks consume `toolUses` in order. Falls back to the legacy grouping when
    // a (legacy) run recorded no blockTypes.
    const ordered: ReactNode[] = [];
    if (m.blockTypes.length === 0) {
        if (thinkingRow) ordered.push(thinkingRow);
        m.toolUses.forEach((t, i) => ordered.push(renderTool(t, i)));
        if (textRow) ordered.push(textRow);
    } else {
        let toolIdx = 0;
        let thinkingDone = false;
        let textDone = false;
        for (const bt of m.blockTypes) {
            if (bt === "thinking") {
                if (!thinkingDone && thinkingRow) ordered.push(thinkingRow);
                thinkingDone = true;
            } else if (bt === "text") {
                if (!textDone && textRow) ordered.push(textRow);
                textDone = true;
            } else if (bt === "tool_use") {
                const t = m.toolUses[toolIdx++];
                if (t) ordered.push(renderTool(t, toolIdx - 1));
            }
        }
        // Defensive: if blockTypes didn't list thinking/text but the aggregated
        // field is present, still render it (thinking first, text last — the
        // legacy positions) so content is never dropped.
        if (!thinkingDone && thinkingRow) ordered.unshift(thinkingRow);
        if (!textDone && textRow) ordered.push(textRow);
    }
    return <>{ordered}</>;
}

// The synthetic reconciliation entry rendered as its own timeline row: tokens
// the agent billed but never surfaced as a generation, booked so the stream's
// token columns sum to the run total. Not expandable (no body), amber-tinted to
// read as a meta row rather than a real LLM call.
function ReconciliationRow({ m, unit }: { m: MessageEvent; unit: Unit }) {
    const fmtTok = (tokens: number | null, kind: TokenKind) =>
        unit === "usd" ? fmtUsd(tokenBucketUsd(m.model, tokens, kind)) : fmtTokens(tokens);
    return (
        <li className="border-b last:border-b-0 border-amber-200 bg-amber-50/40">
            <div className={MSG_GRID + " py-1"}>
                <span aria-hidden="true" />
                <span className="text-amber-700 tabular-nums text-right">{m.index}</span>
                <span className="text-right text-gray-400">—</span>
                <span className="text-right text-gray-400">—</span>
                <span className="flex items-center gap-2 min-w-0">
                    <span className="inline-flex items-center rounded border border-amber-300 bg-amber-100 text-amber-800 px-1.5 py-0.5 text-[10px] font-medium shrink-0">
                        RECONCILE
                    </span>
                    <span
                        className="text-amber-800/90 truncate min-w-0 font-sans"
                        title={m.note ?? undefined}
                    >
                        {m.note ?? "Tokens billed but not surfaced as a generation."}
                    </span>
                </span>
                <span className="tabular-nums text-right text-amber-700">
                    {fmtTok(m.inputTokens, "input")}
                </span>
                <span className="tabular-nums text-right text-amber-700">
                    {fmtTok(m.cacheReadTokens, "cacheRead")}
                </span>
                <span className="tabular-nums text-right text-amber-700">
                    {fmtTok(m.cacheWriteTokens, "cacheWrite")}
                </span>
                <span className="tabular-nums text-right text-amber-700">
                    {fmtTok(m.outputTokens, "output")}
                </span>
                <span className="text-right text-gray-400">—</span>
            </div>
        </li>
    );
}

// A turn the simulator eliminated (all its tools skipped): the generation never
// happens, so its row collapses to a single struck-through line. The token
// columns read "—" because the call's whole footprint (output + its own prompt
// re-read) has been removed from the projected totals — this isn't a row with
// zeroed numbers, it's a row that no longer exists.
function RemovedRow({ m, impact }: { m: MessageEvent; impact: PerMessageImpact }) {
    const tools = impact.toolNames.length > 0 ? impact.toolNames.join(", ") : "tool";
    return (
        <li className="border-b last:border-b-0 border-gray-100 bg-gray-50/60">
            <div className={MSG_GRID + " py-1 text-gray-400"}>
                <span aria-hidden="true" />
                <span className="tabular-nums text-right line-through">
                    {m.index}
                </span>
                <span className="text-right">—</span>
                <span className="text-right">—</span>
                <span className="flex items-center gap-2 min-w-0">
                    <span className="inline-flex items-center rounded border border-gray-300 bg-gray-100 text-gray-500 px-1.5 py-0.5 text-[10px] font-medium shrink-0 font-sans">
                        removed
                    </span>
                    <span className="truncate min-w-0 line-through font-sans">
                        turn skipped ({tools})
                    </span>
                </span>
                <span className="text-right">—</span>
                <span className="text-right">—</span>
                <span className="text-right">—</span>
                <span className="text-right">—</span>
                <span className="text-right">—</span>
            </div>
        </li>
    );
}

// A token-column cell that renders the message's recorded value and, when the
// cost simulator's levers are off as-run, the projected Δ for THAT bucket on a
// second line below — in the same unit as the column (tokens or USD). Green =
// cheaper, rose = dearer; hidden when the bucket doesn't move.
function TokenDeltaCell({
    value,
    delta,
    kind,
    unit,
    model,
}: {
    value: string;
    delta: number | undefined; // projected Δ in tokens for this bucket
    kind: TokenKind;
    unit: Unit;
    model: string | null;
}) {
    const d = delta ?? 0;
    const show = Math.abs(d) >= 1;
    const cheaper = d < 0;
    const usd = unit === "usd" ? tokenBucketUsd(model, d, kind) : null;
    const text = !show
        ? null
        : unit === "usd"
          ? fmtUsd(Math.abs(usd ?? 0))
          : fmtCompact(Math.round(Math.abs(d)));
    return (
        <span className="text-right text-gray-600 leading-tight">
            <span className="tabular-nums block">{value}</span>
            {show && (
                <span
                    className={
                        "tabular-nums block text-[10px] font-medium " +
                        (cheaper ? "text-emerald-600" : "text-rose-600")
                    }
                >
                    {cheaper ? "−" : "+"}
                    {text}
                </span>
            )}
        </span>
    );
}

function MessageRow({
    m,
    unit,
    childrenByParent,
    subAgentUsageByToolId = {},
    impactByIndex,
}: {
    m: MessageEvent;
    unit: Unit;
    // Maps a sub-agent-spawn tool_use_id → the emissions that ran inside that
    // sub-agent. Lets a message render its sub-agents' rows nested between the
    // spawning Agent call and that call's result. Empty for legacy runs.
    childrenByParent: Map<string, MessageEvent[]>;
    // Per-Agent-call sub-agent token breakdown (input/output/cache-create/
    // cache-read), keyed by tool_use_id. Shown on the Agent call's result row.
    subAgentUsageByToolId?: Record<string, SubAgentTotals>;
    // Projected per-message Δ keyed by message index. This row's own Δ shows on
    // its CallTokensRow; the map is threaded down so nested sub-agent calls show
    // theirs too.
    impactByIndex?: Map<number, PerMessageImpact>;
}) {
    const impact = impactByIndex?.get(m.index);
    // Token columns render as counts or, in USD mode, the estimated dollar
    // value of that bucket priced from this message's model.
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
                        className="msg-chevron inline-block w-3 text-gray-400 transition-transform"
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
                        {impact?.eliminated && (
                            <span className="shrink-0 inline-flex items-center rounded border border-orange-200 bg-orange-50 text-orange-700 px-1.5 py-0.5 text-[10px] font-medium font-sans">
                                turn removed
                            </span>
                        )}
                    </span>
                    {/* Token sum + cost intentionally NOT on the expandable
                        summary — they live on the CallTokensRow inside, so each
                        call has one canonical token line. */}
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                </summary>
                <div className="border-t border-gray-100 bg-gray-50/40 divide-y divide-gray-100">
                    <CallTokensRow m={m} unit={unit} impact={impact} />
                    {hasBody && (
                        <MessageBody
                            m={m}
                            unit={unit}
                            childrenByParent={childrenByParent}
                            subAgentUsageByToolId={subAgentUsageByToolId}
                            impactByIndex={impactByIndex}
                        />
                    )}
                </div>
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

export function ConversationSection({ turns }: { turns: ConversationTurn[] }) {
    return (
        <section className="space-y-2">
            <h2 className="text-sm font-semibold text-gray-900">Conversation</h2>
            <div className="space-y-3">
                {turns.map((t, i) => {
                    const isUser = t.role === "USER";
                    return (
                        <div
                            key={i}
                            className={`flex ${isUser ? "justify-end" : "justify-start"}`}
                        >
                            <div
                                className={`max-w-[80%] rounded-lg border p-3 ${
                                    isUser
                                        ? "bg-gray-50 border-gray-200"
                                        : "bg-studio-blue/5 border-studio-blue/20"
                                }`}
                            >
                                <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                                    {t.role} · turn {t.turn}
                                    {t.metadata ? ` · ${t.metadata}` : ""}
                                </div>
                                <div className="whitespace-pre-wrap text-sm text-gray-800 leading-relaxed">
                                    {t.text}
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>
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
