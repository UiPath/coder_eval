"use client";

import { useState } from "react";
import { fmtCompact, fmtUsd } from "@/lib/format";
import {
    type ThinkingModel,
    type ToolStats,
    type TurnProfile,
    computeSkipDelta,
    projectThinking,
    thinkingAmplification,
    toolAmplification,
} from "@/lib/thinkingSim";

const PRESETS = [0, 50, 100, 150, 200];

// Token rows shown in the projected breakdown, top to bottom by typical
// cost weight on a cached agent run.
const ROWS: {
    key: "outputTokens" | "cacheReadTokens" | "cacheCreationTokens" | "inputTokens";
    label: string;
    hint: string;
}[] = [
    { key: "outputTokens", label: "output", hint: "includes thinking tokens" },
    {
        key: "cacheReadTokens",
        label: "cache read",
        hint: "transcript re-read each later call — where the cascade lands",
    },
    { key: "cacheCreationTokens", label: "cache write", hint: "first write of new context" },
    { key: "inputTokens", label: "input", hint: "uncached — held fixed" },
];

function pct(n: number): string {
    const r = Math.round(n);
    return `${r > 0 ? "+" : ""}${r}%`;
}

// Lever state lives in the parent (CostExplorerSection) so the Message timeline
// can render each message's projected Δ inline from the SAME levers. This
// component is fully controlled — it owns no projection state of its own.
export function ThinkingSimulator({
    model,
    scale,
    setScale,
    toolScale,
    setToolScale,
    skippedTools,
    toggleTool,
}: {
    model: ThinkingModel;
    scale: number;
    setScale: (scale: number) => void;
    toolScale: number;
    setToolScale: (scale: number) => void;
    skippedTools: Set<string>;
    toggleTool: (toolName: string) => void;
}) {
    const skip = computeSkipDelta(model.turnProfiles, skippedTools);

    const baseline = projectThinking(model, 1, 1);
    const proj = projectThinking(model, scale, toolScale, skip);
    const delta = proj.costUsd - baseline.costUsd;
    const deltaPct = baseline.costUsd > 0 ? (delta / baseline.costUsd) * 100 : 0;
    const cheaper = delta < -1e-9;
    const dearer = delta > 1e-9;

    const amp = thinkingAmplification(model);
    const toolAmp = toolAmplification(model);
    const thinkShareOfOutput =
        model.outputTokens > 0 ? model.thinkTokens / model.outputTokens : 0;
    const noThinking = model.thinkTokens < 1;
    const noTools = model.toolResultTokens < 1;

    const deltaColor = cheaper
        ? "text-emerald-700"
        : dearer
          ? "text-rose-700"
          : "text-gray-500";

    return (
        <div className="border border-gray-200 rounded-lg bg-white p-4 space-y-4">
            {/* Levers */}
            <div className="space-y-4">
                <ScaleSlider
                    id="think-scale"
                    label="Thinking budget"
                    scale={scale}
                    onChange={setScale}
                    disabled={noThinking}
                    hint="0% = no thinking · 100% = as-run · 200% = double"
                />
                <ScaleSlider
                    id="tool-scale"
                    label="Tool output size"
                    scale={toolScale}
                    onChange={setToolScale}
                    disabled={noTools}
                    hint="0% = no tool output · 100% = as-run · 200% = double"
                />
            </div>

            {noThinking && (
                <div className="text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded p-2">
                    This run generated essentially no thinking tokens, so the
                    thinking lever has nothing to project.
                </div>
            )}
            {noTools && (
                <div className="text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded p-2">
                    This run recorded no tool-result tokens (no tool calls, or a
                    run predating per-tool result sizing), so the tool-output
                    lever has nothing to scale.
                </div>
            )}

            {/* Projected cost headline */}
            <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
                <div>
                    <div className="text-[10px] uppercase tracking-wide text-gray-500">
                        Projected cost
                    </div>
                    <div className="text-2xl font-semibold text-gray-900 tabular-nums">
                        {fmtUsd(proj.costUsd)}
                    </div>
                </div>
                <div>
                    <div className="text-[10px] uppercase tracking-wide text-gray-500">
                        vs as-run
                    </div>
                    <div className={`text-lg font-medium tabular-nums ${deltaColor}`}>
                        {delta >= 0 ? "+" : "−"}
                        {fmtUsd(Math.abs(delta)).replace("$", "$")}
                        <span className="text-sm text-gray-400">
                            {" "}
                            ({pct(deltaPct)})
                        </span>
                    </div>
                </div>
                <div className="text-[11px] text-gray-500 leading-tight">
                    <div>
                        as-run (100%):{" "}
                        <span className="tabular-nums text-gray-700">
                            {fmtUsd(baseline.costUsd)}
                        </span>
                    </div>
                    {model.recordedCostUsd != null && (
                        <div>
                            recorded:{" "}
                            <span className="tabular-nums text-gray-700">
                                {fmtUsd(model.recordedCostUsd)}
                            </span>
                        </div>
                    )}
                </div>
            </div>

            {/* Token breakdown */}
            <div className="border border-gray-200 rounded-lg overflow-hidden">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-500">
                            <th className="py-1.5 px-3 font-medium">Tokens</th>
                            <th className="py-1.5 px-3 font-medium text-right">
                                as-run
                            </th>
                            <th className="py-1.5 px-3 font-medium text-right">
                                projected
                            </th>
                            <th className="py-1.5 px-3 font-medium text-right">
                                Δ
                            </th>
                        </tr>
                    </thead>
                    <tbody className="tabular-nums">
                        {ROWS.map((row) => {
                            const base = baseline[row.key];
                            const now = proj[row.key];
                            const d = now - base;
                            return (
                                <tr
                                    key={row.key}
                                    className="border-b border-gray-100 last:border-b-0"
                                >
                                    <td className="py-1.5 px-3">
                                        <span className="text-gray-800">
                                            {row.label}
                                        </span>
                                        <span className="text-gray-400">
                                            {" "}
                                            · {row.hint}
                                        </span>
                                    </td>
                                    <td className="py-1.5 px-3 text-right text-gray-500">
                                        {fmtCompact(Math.round(base))}
                                    </td>
                                    <td className="py-1.5 px-3 text-right text-gray-900">
                                        {fmtCompact(Math.round(now))}
                                    </td>
                                    <td
                                        className={
                                            "py-1.5 px-3 text-right " +
                                            (Math.abs(d) < 1
                                                ? "text-gray-300"
                                                : d < 0
                                                  ? "text-emerald-700"
                                                  : "text-rose-700")
                                        }
                                    >
                                        {Math.abs(d) < 1
                                            ? "—"
                                            : `${d < 0 ? "−" : "+"}${fmtCompact(Math.round(Math.abs(d)))}`}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {/* Cascade insight */}
            {(!noThinking || !noTools) && (
                <div className="text-[11px] text-gray-600 bg-amber-50/60 border border-amber-200 rounded-lg p-3 space-y-1 leading-relaxed">
                    {!noThinking && (
                        <div>
                            Est.{" "}
                            <span className="font-medium text-gray-800 tabular-nums">
                                {fmtCompact(Math.round(model.thinkTokens))}
                            </span>{" "}
                            thinking tokens across{" "}
                            <span className="tabular-nums">{model.calls}</span>{" "}
                            calls ({Math.round(thinkShareOfOutput * 100)}% of
                            output).
                            {amp != null && (
                                <>
                                    {" "}
                                    Because early-call thinking is re-read from
                                    cache on every later call, each thinking
                                    token costs{" "}
                                    <span className="font-semibold text-amber-800 tabular-nums">
                                        {amp.toFixed(1)}×
                                    </span>{" "}
                                    its face output price over the run.
                                </>
                            )}
                        </div>
                    )}
                    {!noTools && (
                        <div>
                            Est.{" "}
                            <span className="font-medium text-gray-800 tabular-nums">
                                {fmtCompact(Math.round(model.toolResultTokens))}
                            </span>{" "}
                            tool-result tokens injected into the transcript.
                            {toolAmp != null && (
                                <>
                                    {" "}
                                    Written once then re-read every later call,
                                    each costs{" "}
                                    <span className="font-semibold text-amber-800 tabular-nums">
                                        {toolAmp.toFixed(1)}×
                                    </span>{" "}
                                    a single cache write — the cache-read row is
                                    where the swing lands.
                                </>
                            )}
                        </div>
                    )}
                </div>
            )}

            {/* Skip tools */}
            {model.toolStats.length > 0 && (
                <ToolSkipSection
                    toolStats={model.toolStats}
                    turnProfiles={model.turnProfiles}
                    skippedTools={skippedTools}
                    onToggle={toggleTool}
                    model={model}
                    baseline={baseline}
                    scale={scale}
                    toolScale={toolScale}
                />
            )}

            {/* Method caveat */}
            <p className="text-[10px] text-gray-400 leading-snug">
                Priced as <span className="font-mono">{model.model}</span>.
                Thinking volume is estimated from the thinking-time share of
                generation (the SDK doesn&apos;t report thinking tokens directly);
                tool-result volume is estimated as the cache growth each call
                adds beyond the model&apos;s own output (result sizes aren&apos;t
                recorded). The trajectory itself — number of calls and tool calls
                — is held fixed: a what-if on volume, not a re-run.
            </p>
        </div>
    );
}

// Checklist of tools with call counts. Checked tools are treated as completely
// removed, which has two effects on the projection:
//
//   1. Direct token savings — the output tokens the model spent generating the
//      tool call (writing tool name + parameters) are subtracted from the output
//      bill. Result-size savings come from each tool's estimated share of
//      injected tool-result tokens, derived from result preview lengths.
//
//   2. Turn elimination — when EVERY tool in an API call belongs to the skipped
//      set, that entire call would never have been made. The thinking generated
//      in that call also disappears: both the output tokens charged at call time
//      and the cascade cost of that thinking being re-read on every later call.
//      Calls with mixed tools (some skipped, some kept) are left intact
//      conservatively — we can't know how the agent would have restructured.
//
// The "eliminates N turns" badge on each tool row counts the calls where that
// tool is the ONLY tool (solo turn-eliminating call), giving a quick read on
// how much "wasted" API round-trips a tool is responsible for.
function ToolSkipSection({
    toolStats,
    turnProfiles,
    skippedTools,
    onToggle,
    model,
    baseline,
    scale,
    toolScale,
}: {
    toolStats: ToolStats[];
    turnProfiles: TurnProfile[];
    skippedTools: Set<string>;
    onToggle: (toolName: string) => void;
    model: ThinkingModel;
    baseline: ReturnType<typeof projectThinking>;
    scale: number;
    toolScale: number;
}) {
    const [open, setOpen] = useState(false);

    // For each tool: how many turns does it SOLELY occupy (only tool in that call)?
    // Those turns are guaranteed to be eliminated when the tool is skipped —
    // regardless of what else is checked. Mixed-tool turns only become eliminated
    // when ALL their tools are checked.
    const soloTurnCount = new Map<string, number>();
    for (const tp of turnProfiles) {
        if (tp.toolNames.length === 1) {
            soloTurnCount.set(tp.toolNames[0], (soloTurnCount.get(tp.toolNames[0]) ?? 0) + 1);
        }
    }

    // Helper: project cost given a specific skipped-tool set (used to compute
    // marginal savings for each row independently).
    function projectForSet(skipped: Set<string>): number {
        return projectThinking(model, scale, toolScale, computeSkipDelta(turnProfiles, skipped))
            .costUsd;
    }

    // Marginal saving of toggling each tool (cost without it minus cost with it),
    // holding all other current selections fixed.
    const perToolSavings = toolStats.map((t) => {
        const withSet = new Set(skippedTools);
        const withoutSet = new Set(skippedTools);
        if (skippedTools.has(t.toolName)) {
            withSet.delete(t.toolName); // "with" = restore it
        } else {
            withoutSet.add(t.toolName); // "without" = add it
        }
        return {
            toolName: t.toolName,
            saving: projectForSet(withSet) - projectForSet(withoutSet),
        };
    });

    const totalSkippedSaving = baseline.costUsd - projectForSet(skippedTools);

    return (
        <div className="border border-gray-200 rounded-lg overflow-hidden">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center justify-between px-3 py-2 bg-gray-50 hover:bg-gray-100 transition-colors text-left"
            >
                <span className="text-xs font-medium text-gray-700">
                    Skip tools
                    {skippedTools.size > 0 && (
                        <span className="ml-2 px-1.5 py-0.5 bg-orange-100 text-orange-700 rounded text-[10px] font-semibold">
                            {skippedTools.size} skipped
                        </span>
                    )}
                </span>
                <div className="flex items-center gap-3">
                    {skippedTools.size > 0 && totalSkippedSaving > 1e-6 && (
                        <span className="text-[11px] font-medium text-emerald-700 tabular-nums">
                            −{fmtUsd(totalSkippedSaving)}
                        </span>
                    )}
                    <span className="text-gray-400 text-xs">{open ? "▲" : "▼"}</span>
                </div>
            </button>
            {open && (
                <div className="divide-y divide-gray-100">
                    <div className="px-3 py-2 text-[10px] text-gray-500 bg-white leading-relaxed">
                        Check tools to remove them entirely. Savings include: generation
                        tokens (writing the call), result tokens (injected into context),
                        and — when a call&apos;s only tools are all skipped — the thinking
                        generated in that call plus its downstream cache-read cascade.
                        Mixed calls (some skipped tools, some kept) are conservatively
                        left intact.
                    </div>
                    {toolStats.map((t) => {
                        const checked = skippedTools.has(t.toolName);
                        const saving = perToolSavings.find((s) => s.toolName === t.toolName)?.saving ?? 0;
                        const soloTurns = soloTurnCount.get(t.toolName) ?? 0;
                        return (
                            <label
                                key={t.toolName}
                                className={
                                    "flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-gray-50 transition-colors " +
                                    (checked ? "bg-orange-50/60" : "bg-white")
                                }
                            >
                                <input
                                    type="checkbox"
                                    checked={checked}
                                    onChange={() => onToggle(t.toolName)}
                                    className="accent-uipath-orange"
                                />
                                <span className="flex-1 text-xs font-mono text-gray-800">
                                    {t.toolName}
                                </span>
                                {soloTurns > 0 && (
                                    <span
                                        title={`${soloTurns} API call(s) exist solely to run this tool — skipping it eliminates those turns entirely, including their thinking`}
                                        className="text-[10px] text-amber-600 bg-amber-50 border border-amber-200 rounded px-1 py-0.5 tabular-nums"
                                    >
                                        {soloTurns} turn{soloTurns !== 1 ? "s" : ""}
                                    </span>
                                )}
                                <span className="text-[11px] text-gray-400 tabular-nums">
                                    {t.callCount}×
                                </span>
                                <span
                                    className={
                                        "text-[11px] tabular-nums w-16 text-right " +
                                        (saving > 1e-6 ? "text-emerald-700" : "text-gray-300")
                                    }
                                >
                                    {saving > 1e-6 ? `−${fmtUsd(saving)}` : "—"}
                                </span>
                            </label>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// One 0–200% lever. Renders the labelled range + preset chips and reports the
// scale (1 = as-run) back through onChange. Disabled levers grey out and pin.
function ScaleSlider({
    id,
    label,
    scale,
    onChange,
    disabled,
    hint,
}: {
    id: string;
    label: string;
    scale: number;
    onChange: (scale: number) => void;
    disabled: boolean;
    hint: string;
}) {
    return (
        <div className="space-y-2">
            <div className="flex items-baseline justify-between gap-3">
                <label htmlFor={id} className="text-sm font-medium text-gray-900">
                    {label}
                </label>
                <span className="text-sm tabular-nums font-semibold text-gray-900">
                    {Math.round(scale * 100)}%
                    <span className="text-gray-400 font-normal"> of as-run</span>
                </span>
            </div>
            <input
                id={id}
                type="range"
                min={0}
                max={200}
                step={5}
                value={Math.round(scale * 100)}
                onChange={(e) => onChange(Number(e.target.value) / 100)}
                className="w-full accent-uipath-orange"
                disabled={disabled}
            />
            <div className="flex items-center justify-between">
                <div className="flex gap-1.5">
                    {PRESETS.map((p) => (
                        <button
                            key={p}
                            type="button"
                            onClick={() => onChange(p / 100)}
                            className={
                                "px-2 py-0.5 text-[11px] rounded border tabular-nums transition-colors " +
                                (Math.round(scale * 100) === p
                                    ? "border-uipath-orange text-uipath-orange bg-orange-50"
                                    : "border-gray-200 text-gray-500 hover:bg-gray-50")
                            }
                            disabled={disabled}
                        >
                            {p}%
                        </button>
                    ))}
                </div>
                <span className="text-[10px] text-gray-400">{hint}</span>
            </div>
        </div>
    );
}
