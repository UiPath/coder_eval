"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { TaskResultSummary } from "@/lib/runs";
import type { ReviewIndexEntry } from "@/lib/reviews-types";
import { fmtCompact, fmtUsd, humanizeTaskId } from "@/lib/format";
import { tokenBucketUsd, type TokenKind } from "@/lib/pricing";
import { type Unit, UnitToggle } from "@/app/_components/unit-toggle";
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
import {
    type ColHelp,
    HelpPopover,
    TOKEN_COLUMN_HELP,
} from "@/app/_components/col-help";

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

// Per-column help shown from an ⓘ next to the header. Token-column copy is
// shared with the message timeline via TOKEN_COLUMN_HELP; Cost is grid-specific
// (the authoritative SDK total for the task).
const COLUMN_HELP: Partial<Record<SortKey, ColHelp>> = {
    ...TOKEN_COLUMN_HELP,
    cost: {
        title: "Cost (USD)",
        body: "Total billed cost for this task, reported by the SDK (summed across turns).",
        causes: "long runs, large context replayed each turn, verbose output, or an expensive model.",
        fix: "fewer turns, less context, more concise output; use a cheaper model where acceptable.",
    },
};

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

// Render a token column either as a compact token count or, in USD mode, as the
// estimated dollar value of that bucket priced from the task's model.
function tokenCell(
    unit: Unit,
    model: string | null,
    tokens: number | null,
    kind: TokenKind,
): string {
    if (unit === "usd") return fmtUsd(tokenBucketUsd(model, tokens, kind));
    return tokens != null ? fmtCompact(tokens) : "—";
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
    { key: "cr", header: "Cache R", align: "right" },
    { key: "cw", header: "Cache W", align: "right" },
    { key: "output", header: "Out", align: "right" },
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

    // Token columns can be shown as counts or as their estimated USD value.
    const [unit, setUnit] = useState<Unit>("tokens");

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
        <div className="space-y-2">
            <div className="flex justify-end">
                <UnitToggle value={unit} onChange={setUnit} />
            </div>
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
                                title="cache_read_input_tokens (cached tokens re-billed each turn — usually the dominant cost line)"
                            >
                                {tokenCell(
                                    unit,
                                    t.model,
                                    t.cacheReadTokens,
                                    "cacheRead",
                                )}
                            </td>
                            <td
                                className="py-3 px-4 text-right tabular-nums text-gray-700"
                                title="cache_creation_input_tokens (tokens written to cache this task)"
                            >
                                {tokenCell(
                                    unit,
                                    t.model,
                                    t.cacheCreationTokens,
                                    "cacheWrite",
                                )}
                            </td>
                            <td
                                className="py-3 px-4 text-right tabular-nums text-gray-700"
                                title="output_tokens"
                            >
                                {tokenCell(
                                    unit,
                                    t.model,
                                    t.outputTokens,
                                    "output",
                                )}
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
        </div>
    );
}
