"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { TaskResultSummary } from "@/lib/runs";
import type { ReviewIndexEntry } from "@/lib/reviews-types";
import { fmtCompact, humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";
import { statusSortRank } from "@/lib/status";
import {
    displayedTurns,
    fmtTurnsCount,
    tintForRatio,
    turnRatio,
    turnsCellClasses,
} from "@/lib/turns";
import { ChipButton } from "./chips";

type SortKey =
    | "task"
    | "status"
    | "score"
    | "duration"
    | "cost"
    | "turns"
    | "output"
    | "cw"
    | "cr";

// ---- Token-column help: what the number is, what drives it up, how to bring
// it down. Rendered as a static, selectable popover from an ⓘ next to the
// header (click to open, click-outside / Esc to close).
type ColHelp = {
    title: string;
    body: string;
    causes?: string; // common causes of high values
    fix?: string; // potential fixes
};

const COLUMN_HELP: Partial<Record<SortKey, ColHelp>> = {
    output: {
        title: "Output tokens",
        body: "Text, code, tool arguments and reasoning the model generated.",
        causes: "verbose final answers, large file rewrites, heavy reasoning.",
        fix: "ask for concise output, scope edits to smaller diffs, cap max_output_tokens / max_turns.",
    },
    cw: {
        title: "Cache-write tokens",
        body: "Context written into the prompt cache this task (cache_creation_input_tokens).",
        causes: "the cached prefix keeps changing — new files read mid-run, a growing transcript — so it's re-written instead of reused.",
        fix: "keep stable content (system prompt, skills, instructions) at the front of the prompt; don't inject volatile content early; reuse sessions.",
    },
    cr: {
        title: "Cache-read tokens",
        body: "Cached input re-billed every turn (cache_read_input_tokens). Usually the dominant cost line.",
        causes: "large context (big files, long transcript, many skills/tools) replayed on every turn × many turns.",
        fix: "put less in context (smaller file reads, fewer files), shorten the run (fewer turns), trim system/skill payloads, compact long transcripts.",
    },
};

function HelpPopover({ help, align }: { help: ColHelp; align: "left" | "right" }) {
    return (
        <div
            role="tooltip"
            // Anchor under the ⓘ; align to the same edge as the column text so
            // it stays inside the table on the right-aligned token columns.
            className={`absolute top-full z-20 mt-1.5 w-72 cursor-auto rounded-md border border-gray-200 bg-white p-3 text-left text-xs font-normal leading-snug text-gray-600 shadow-lg ${
                align === "right" ? "right-0" : "left-0"
            }`}
            // Keep clicks inside the card from sorting / closing.
            onClick={(e) => e.stopPropagation()}
        >
            <div className="font-semibold text-gray-900">{help.title}</div>
            <p className="mt-1">{help.body}</p>
            {help.causes && (
                <p className="mt-2">
                    <span className="font-medium text-gray-700">
                        Common causes:
                    </span>{" "}
                    {help.causes}
                </p>
            )}
            {help.fix && (
                <p className="mt-1">
                    <span className="font-medium text-gray-700">
                        Reduce by:
                    </span>{" "}
                    {help.fix}
                </p>
            )}
        </div>
    );
}

function fmtTableDuration(s: number | null): string {
    if (s == null) return "—";
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    const rem = s - m * 60;
    return `${m}m${rem.toFixed(0).padStart(2, "0")}s`;
}

function fmtCost(c: number | null): string {
    if (c == null) return "—";
    return `$${c.toFixed(3)}`;
}

const DEFAULT_DIR: Record<SortKey, "asc" | "desc"> = {
    task: "asc",
    status: "asc",
    score: "desc",
    duration: "desc",
    cost: "desc",
    turns: "desc",
    output: "desc",
    cw: "desc",
    cr: "desc",
};

function compare(
    a: TaskResultSummary,
    b: TaskResultSummary,
    key: SortKey,
): number {
    switch (key) {
        case "task":
            return a.taskId.localeCompare(b.taskId);
        case "status":
            return statusSortRank(a.status) - statusSortRank(b.status);
        case "score":
            return (
                (a.weightedScore ?? -Infinity) -
                (b.weightedScore ?? -Infinity)
            );
        case "duration":
            return (
                (a.durationSeconds ?? -Infinity) -
                (b.durationSeconds ?? -Infinity)
            );
        case "cost":
            return (
                (a.totalCostUsd ?? -Infinity) - (b.totalCostUsd ?? -Infinity)
            );
        case "turns":
            return (
                (displayedTurns(a.actualCommands, a.hasFinalReply) ??
                    -Infinity) -
                (displayedTurns(b.actualCommands, b.hasFinalReply) ??
                    -Infinity)
            );
        case "output":
            return (a.outputTokens ?? -Infinity) - (b.outputTokens ?? -Infinity);
        case "cw":
            return (
                (a.cacheCreationTokens ?? -Infinity) -
                (b.cacheCreationTokens ?? -Infinity)
            );
        case "cr":
            return (
                (a.cacheReadTokens ?? -Infinity) -
                (b.cacheReadTokens ?? -Infinity)
            );
    }
}

const COLUMNS: Array<{
    key: SortKey;
    header: string;
    align?: "right";
}> = [
    { key: "task", header: "Task" },
    { key: "status", header: "Status" },
    { key: "score", header: "Score", align: "right" },
    { key: "duration", header: "Duration", align: "right" },
    { key: "cost", header: "Cost", align: "right" },
    { key: "turns", header: "Turns", align: "right" },
    { key: "output", header: "Out", align: "right" },
    { key: "cw", header: "Cache+", align: "right" },
    { key: "cr", header: "Cache↺", align: "right" },
];

export function TaskGrid({
    runId,
    tasks,
    selectedSet,
    onToggleTag,
    emptyHint = "no tasks in this run",
    reviewsByTask,
    reviewSelectedSet,
    onToggleReviewTag,
}: {
    runId: string;
    tasks: TaskResultSummary[];
    selectedSet?: Set<string>;
    onToggleTag?: (tag: string) => void;
    emptyHint?: string;
    reviewsByTask?: Map<string, ReviewIndexEntry>;
    reviewSelectedSet?: Set<string>;
    onToggleReviewTag?: (tag: string) => void;
}) {
    const [sort, setSort] = useState<{
        key: SortKey;
        dir: "asc" | "desc";
    } | null>(null);

    // Which column's help popover is open (one at a time). Dismissed by a click
    // outside any popover/trigger or by Escape.
    const [openHelp, setOpenHelp] = useState<SortKey | null>(null);
    useEffect(() => {
        if (openHelp == null) return;
        const onDown = (e: MouseEvent) => {
            const el = e.target as Element | null;
            if (!el?.closest("[data-col-help]")) setOpenHelp(null);
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") setOpenHelp(null);
        };
        document.addEventListener("mousedown", onDown);
        document.addEventListener("keydown", onKey);
        return () => {
            document.removeEventListener("mousedown", onDown);
            document.removeEventListener("keydown", onKey);
        };
    }, [openHelp]);

    const sorted = useMemo(() => {
        const arr = [...tasks];
        if (sort) {
            arr.sort((a, b) => {
                const c = compare(a, b, sort.key);
                if (c !== 0) return sort.dir === "asc" ? c : -c;
                return a.taskId.localeCompare(b.taskId);
            });
        } else {
            // Default: failures first, then by task id.
            arr.sort(
                (a, b) =>
                    statusSortRank(a.status) - statusSortRank(b.status) ||
                    a.taskId.localeCompare(b.taskId),
            );
        }
        return arr;
    }, [tasks, sort]);

    const onSort = (key: SortKey) => {
        setSort((cur) =>
            cur?.key === key
                ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
                : { key, dir: DEFAULT_DIR[key] },
        );
    };

    return (
        <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
            <table className="w-full text-sm">
                <thead>
                    <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                        {COLUMNS.map((col) => {
                            const alignCls =
                                col.align === "right"
                                    ? "text-right"
                                    : "text-left";
                            const active = sort?.key === col.key;
                            const arrow = active
                                ? sort.dir === "asc"
                                    ? "▲"
                                    : "▼"
                                : "";
                            const ariaSort: "ascending" | "descending" | "none" =
                                active
                                    ? sort.dir === "asc"
                                        ? "ascending"
                                        : "descending"
                                    : "none";
                            const help = COLUMN_HELP[col.key];
                            return (
                                <th
                                    key={col.key}
                                    aria-sort={ariaSort}
                                    className={`py-3 px-4 font-medium ${alignCls}`}
                                >
                                    <span
                                        className={`inline-flex items-center gap-1 ${
                                            col.align === "right"
                                                ? "flex-row-reverse"
                                                : ""
                                        }`}
                                    >
                                        <button
                                            type="button"
                                            onClick={() => onSort(col.key)}
                                            className="inline-flex items-center gap-1 hover:text-gray-900"
                                        >
                                            {col.header}
                                            <span className="text-xs text-gray-400 w-3">
                                                {arrow}
                                            </span>
                                        </button>
                                        {help && (
                                            <span
                                                data-col-help
                                                className="relative inline-flex"
                                            >
                                                <button
                                                    type="button"
                                                    aria-label={`What is ${col.header}?`}
                                                    aria-expanded={
                                                        openHelp === col.key
                                                    }
                                                    onClick={() =>
                                                        setOpenHelp((cur) =>
                                                            cur === col.key
                                                                ? null
                                                                : col.key,
                                                        )
                                                    }
                                                    className={`flex h-4 w-4 items-center justify-center rounded-full border text-[10px] font-semibold leading-none transition-colors ${
                                                        openHelp === col.key
                                                            ? "border-studio-blue text-studio-blue"
                                                            : "border-gray-300 text-gray-400 hover:border-gray-400 hover:text-gray-600"
                                                    }`}
                                                >
                                                    i
                                                </button>
                                                {openHelp === col.key && (
                                                    // All help columns sit on
                                                    // the right side of the
                                                    // table; open leftward so
                                                    // the card stays inside the
                                                    // overflow-hidden container.
                                                    <HelpPopover
                                                        help={help}
                                                        align="right"
                                                    />
                                                )}
                                            </span>
                                        )}
                                    </span>
                                </th>
                            );
                        })}
                    </tr>
                </thead>
                <tbody>
                    {sorted.map((t) => {
                        const review = reviewsByTask?.get(t.taskId);
                        // Color off the same visible-events count the cell
                        // displays — not SDK num_turns (totalTurns), which the
                        // visible-turns refactor dropped from the display and
                        // is often null, leaving the cell silently uncolored.
                        const turnsTint = tintForRatio(
                            turnRatio(
                                displayedTurns(t.actualCommands, t.hasFinalReply),
                                t.expectedTurns,
                            ),
                        );
                        return (
                        <tr
                            key={t.taskId}
                            className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                        >
                            <td className="py-3 px-4 text-gray-700">
                                <div className="flex flex-col min-w-0 gap-0.5">
                                    <Link
                                        href={`/runs/${runId}/${t.taskId}`}
                                        className="text-gray-900 hover:text-studio-blue font-semibold"
                                    >
                                        {humanizeTaskId(t.taskId)}
                                    </Link>
                                    {(t.skill ||
                                        t.tags.length > 0 ||
                                        (review && review.tags.length > 0)) && (
                                        <div className="flex flex-wrap gap-1 mt-0.5">
                                            {t.skill && (
                                                <ChipButton
                                                    key={`s:${t.skill}`}
                                                    tag={t.skill}
                                                    variant="skill"
                                                    size="sm"
                                                    active={
                                                        selectedSet?.has(
                                                            t.skill,
                                                        ) ?? false
                                                    }
                                                    onClick={
                                                        onToggleTag
                                                            ? () =>
                                                                  onToggleTag(
                                                                      t.skill!,
                                                                  )
                                                            : undefined
                                                    }
                                                />
                                            )}
                                            {review?.tags.map((tag) => (
                                                <ChipButton
                                                    key={`r:${tag}`}
                                                    tag={tag}
                                                    variant="review"
                                                    size="sm"
                                                    active={
                                                        reviewSelectedSet?.has(
                                                            tag,
                                                        ) ?? false
                                                    }
                                                    onClick={
                                                        onToggleReviewTag
                                                            ? () =>
                                                                  onToggleReviewTag(
                                                                      tag,
                                                                  )
                                                            : undefined
                                                    }
                                                    title={
                                                        review.summary_excerpt
                                                    }
                                                />
                                            ))}
                                            {t.tags
                                                .filter(
                                                    (tag) => tag !== t.skill,
                                                )
                                                .map((tag) => (
                                                    <ChipButton
                                                        key={`t:${tag}`}
                                                        tag={tag}
                                                        variant="tag"
                                                        size="sm"
                                                        active={
                                                            selectedSet?.has(
                                                                tag,
                                                            ) ?? false
                                                        }
                                                        onClick={
                                                            onToggleTag
                                                                ? () =>
                                                                      onToggleTag(
                                                                          tag,
                                                                      )
                                                                : undefined
                                                        }
                                                    />
                                                ))}
                                        </div>
                                    )}
                                </div>
                            </td>
                            <td className="py-3 px-4">
                                <StatusPill status={t.status} relabel />
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {t.weightedScore != null
                                    ? t.weightedScore.toFixed(2)
                                    : "—"}
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {fmtTableDuration(t.durationSeconds)}
                            </td>
                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                {fmtCost(t.totalCostUsd)}
                            </td>
                            <td
                                className={`py-3 px-4 text-right tabular-nums font-medium ${turnsCellClasses(turnsTint)}`}
                                title={
                                    t.expectedTurns != null
                                        ? `expected_turns target: ${t.expectedTurns}`
                                        : "no expected_turns target set"
                                }
                            >
                                {fmtTurnsCount(
                                    displayedTurns(
                                        t.actualCommands,
                                        t.hasFinalReply,
                                    ),
                                )}
                            </td>
                            <td
                                className="py-3 px-4 text-right tabular-nums text-gray-700"
                                title="output_tokens"
                            >
                                {t.outputTokens != null
                                    ? fmtCompact(t.outputTokens)
                                    : "—"}
                            </td>
                            <td
                                className="py-3 px-4 text-right tabular-nums text-gray-700"
                                title="cache_creation_input_tokens (tokens written to cache this task)"
                            >
                                {t.cacheCreationTokens != null
                                    ? fmtCompact(t.cacheCreationTokens)
                                    : "—"}
                            </td>
                            <td
                                className="py-3 px-4 text-right tabular-nums text-gray-700"
                                title="cache_read_input_tokens (cached tokens re-billed each turn — usually the dominant cost line)"
                            >
                                {t.cacheReadTokens != null
                                    ? fmtCompact(t.cacheReadTokens)
                                    : "—"}
                            </td>
                        </tr>
                        );
                    })}
                    {sorted.length === 0 && (
                        <tr>
                            <td
                                colSpan={COLUMNS.length}
                                className="py-6 px-4 text-center text-sm text-gray-500"
                            >
                                {emptyHint}
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
