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
import { TableScroll } from "@/app/_components/scroll-table";
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
    | "input"
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
    input: "desc",
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
        case "input":
            return (a.inputTokens ?? -Infinity) - (b.inputTokens ?? -Infinity);
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
    { key: "input", header: "In", align: "right" },
    { key: "cr", header: "Cache R", align: "right" },
    { key: "cw", header: "Cache W", align: "right" },
    { key: "output", header: "Out", align: "right" },
];

// The four token columns fold into one collapsible group. They're the densest
// part of the grid and the first thing to overflow a narrow screen, so they're
// hidden by default on small viewports (shown on desktop) and toggled on demand
// — the data isn't removed, just tucked away until asked for.
const TOKEN_KEYS = new Set<SortKey>(["input", "cr", "cw", "output"]);

// Skill / review / tag chips for a task. Shared by the table row and the mobile
// card so the two stay in lock-step.
function TaskTagChips({
    t,
    review,
    selectedSet,
    onToggleTag,
    reviewSelectedSet,
    onToggleReviewTag,
}: {
    t: TaskResultSummary;
    review?: ReviewIndexEntry;
    selectedSet?: Set<string>;
    onToggleTag?: (tag: string) => void;
    reviewSelectedSet?: Set<string>;
    onToggleReviewTag?: (tag: string) => void;
}) {
    if (!(t.skill || t.tags.length > 0 || (review && review.tags.length > 0))) {
        return null;
    }
    return (
        <div className="flex flex-wrap gap-1 mt-0.5">
            {t.skill && (
                <ChipButton
                    key={`s:${t.skill}`}
                    tag={t.skill}
                    variant="skill"
                    size="sm"
                    active={selectedSet?.has(t.skill) ?? false}
                    onClick={
                        onToggleTag ? () => onToggleTag(t.skill!) : undefined
                    }
                />
            )}
            {review?.tags.map((tag) => (
                <ChipButton
                    key={`r:${tag}`}
                    tag={tag}
                    variant="review"
                    size="sm"
                    active={reviewSelectedSet?.has(tag) ?? false}
                    onClick={
                        onToggleReviewTag
                            ? () => onToggleReviewTag(tag)
                            : undefined
                    }
                    title={review.summary_excerpt}
                />
            ))}
            {t.tags
                .filter((tag) => tag !== t.skill)
                .map((tag) => (
                    <ChipButton
                        key={`t:${tag}`}
                        tag={tag}
                        variant="tag"
                        size="sm"
                        active={selectedSet?.has(tag) ?? false}
                        onClick={
                            onToggleTag ? () => onToggleTag(tag) : undefined
                        }
                    />
                ))}
        </div>
    );
}

// One label/value pair in a mobile card's metric grid.
function Stat({
    label,
    value,
    valueClass = "text-gray-800",
}: {
    label: string;
    value: string;
    valueClass?: string;
}) {
    return (
        <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-gray-400">
                {label}
            </div>
            <div className={`tabular-nums ${valueClass}`}>{value}</div>
        </div>
    );
}

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

    // Whether the Cache R / Cache W / Out group is expanded. Collapsed by
    // default on every screen — the grid opens to the 6 essential columns and
    // the token detail is one click away via the toolbar toggle.
    const [showTokens, setShowTokens] = useState(false);

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

    const visibleColumns = COLUMNS.filter(
        (c) => showTokens || !TOKEN_KEYS.has(c.key),
    );

    return (
        <div className="space-y-2">
            <div className="flex justify-end items-center gap-2">
                {showTokens && <UnitToggle value={unit} onChange={setUnit} />}
                <button
                    type="button"
                    onClick={() => setShowTokens((v) => !v)}
                    aria-expanded={showTokens}
                    className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-0.5 text-xs text-gray-600 hover:bg-gray-50 hover:text-gray-900 transition-colors"
                >
                    <span
                        aria-hidden
                        className={`inline-block transition-transform ${showTokens ? "rotate-90" : ""}`}
                    >
                        ▸
                    </span>
                    {showTokens ? "Hide tokens" : "Show tokens"}
                </button>
            </div>
            <TableScroll className="hidden md:block">
            <table className="w-full text-sm">
                <thead>
                    <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                        {visibleColumns.map((col) => {
                            const alignCls =
                                col.align === "right"
                                    ? "text-right"
                                    : "text-left";
                            // Pin the Task column so the metric columns can scroll
                            // under it on narrow screens without losing the name.
                            const stickyCls =
                                col.key === "task"
                                    ? "sticky left-0 z-10 bg-gray-50"
                                    : "";
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
                                    className={`py-3 px-4 font-medium ${alignCls} ${stickyCls}`}
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
                            className="group border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                        >
                            <td className="py-3 px-4 text-gray-700 sticky left-0 z-10 bg-white group-hover:bg-gray-50">
                                <div className="flex flex-col min-w-0 gap-0.5">
                                    <Link
                                        href={`/runs/${runId}/${t.taskId}`}
                                        className="text-gray-900 hover:text-studio-blue font-semibold"
                                    >
                                        {humanizeTaskId(t.taskId)}
                                    </Link>
                                    <TaskTagChips
                                        t={t}
                                        review={review}
                                        selectedSet={selectedSet}
                                        onToggleTag={onToggleTag}
                                        reviewSelectedSet={reviewSelectedSet}
                                        onToggleReviewTag={onToggleReviewTag}
                                    />
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
                            {showTokens && (
                                <>
                                    <td
                                        className="py-3 px-4 text-right tabular-nums text-gray-700"
                                        title="uncached_input_tokens (fresh prompt input billed at the full input rate)"
                                    >
                                        {tokenCell(
                                            unit,
                                            t.model,
                                            t.inputTokens,
                                            "input",
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
                                </>
                            )}
                        </tr>
                        );
                    })}
                    {sorted.length === 0 && (
                        <tr>
                            <td
                                colSpan={visibleColumns.length}
                                className="py-6 px-4 text-center text-sm text-gray-500"
                            >
                                {emptyHint}
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
            </TableScroll>

            {/* Mobile: the same rows as stacked cards. A wide metric table is
                miserable on a phone (you can scroll it, but you see two columns
                at a time), so below md each task becomes a card with its metrics
                in a small grid — and the token group folds in exactly like the
                table's columns do. */}
            <div className="md:hidden space-y-2">
                {sorted.map((t) => {
                    const review = reviewsByTask?.get(t.taskId);
                    const turnsTint = tintForRatio(
                        turnRatio(
                            displayedTurns(t.actualCommands, t.hasFinalReply),
                            t.expectedTurns,
                        ),
                    );
                    return (
                        <div
                            key={t.taskId}
                            className="rounded-lg border border-gray-200 bg-white p-3 space-y-2"
                        >
                            <div className="flex items-start justify-between gap-2">
                                <Link
                                    href={`/runs/${runId}/${t.taskId}`}
                                    className="min-w-0 break-words font-semibold text-gray-900 hover:text-studio-blue"
                                >
                                    {humanizeTaskId(t.taskId)}
                                </Link>
                                <span className="shrink-0">
                                    <StatusPill status={t.status} relabel />
                                </span>
                            </div>
                            <TaskTagChips
                                t={t}
                                review={review}
                                selectedSet={selectedSet}
                                onToggleTag={onToggleTag}
                                reviewSelectedSet={reviewSelectedSet}
                                onToggleReviewTag={onToggleReviewTag}
                            />
                            <dl className="grid grid-cols-4 gap-2 pt-1 text-xs">
                                <Stat
                                    label="Score"
                                    value={
                                        t.weightedScore != null
                                            ? t.weightedScore.toFixed(2)
                                            : "—"
                                    }
                                />
                                <Stat
                                    label="Duration"
                                    value={fmtTableDuration(t.durationSeconds)}
                                />
                                <Stat
                                    label="Cost"
                                    value={fmtCost(t.totalCostUsd)}
                                />
                                <Stat
                                    label="Turns"
                                    value={fmtTurnsCount(
                                        displayedTurns(
                                            t.actualCommands,
                                            t.hasFinalReply,
                                        ),
                                    )}
                                    valueClass={`font-medium ${turnsCellClasses(turnsTint)}`}
                                />
                            </dl>
                            {showTokens && (
                                <dl className="grid grid-cols-3 gap-2 border-t border-gray-100 pt-2 text-xs">
                                    <Stat
                                        label="Cache R"
                                        value={tokenCell(
                                            unit,
                                            t.model,
                                            t.cacheReadTokens,
                                            "cacheRead",
                                        )}
                                    />
                                    <Stat
                                        label="Cache W"
                                        value={tokenCell(
                                            unit,
                                            t.model,
                                            t.cacheCreationTokens,
                                            "cacheWrite",
                                        )}
                                    />
                                    <Stat
                                        label="Out"
                                        value={tokenCell(
                                            unit,
                                            t.model,
                                            t.outputTokens,
                                            "output",
                                        )}
                                    />
                                </dl>
                            )}
                        </div>
                    );
                })}
                {sorted.length === 0 && (
                    <div className="rounded-lg border border-gray-200 bg-white py-6 px-4 text-center text-sm text-gray-500">
                        {emptyHint}
                    </div>
                )}
            </div>
        </div>
    );
}
