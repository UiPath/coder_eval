"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { TaskResultSummary } from "@/lib/runs";
import type { ReviewIndexEntry } from "@/lib/reviews-types";
import { fmtCompact, fmtRunTime, fmtUsd, humanizeTaskId } from "@/lib/format";
import { tokenBucketUsd, type TokenKind } from "@/lib/pricing";
import { type Unit, UnitToggle } from "@/app/_components/unit-toggle";
import {
    matureLinkTooltip,
    MATURE_NO_SOURCE_TOOLTIP,
    MaturePill,
    StatusPill,
} from "@/lib/pills";
import {
    collapseReplicates,
    perTaskPassCounts,
    statusSortRank,
    taskGroupKey,
} from "@/lib/status";
import {
    displayedTurns,
    fmtTurnsCount,
    tintForRatio,
    turnRatio,
    turnsCellClasses,
} from "@/lib/turns";
import { DEFAULT_VARIANT } from "@/lib/variant";
import { ChipButton } from "./chips";
import { TableScroll } from "@/app/_components/scroll-table";
import {
    type ColHelp,
    HelpPopover,
    TOKEN_COLUMN_HELP,
} from "@/app/_components/col-help";

type SortKey =
    | "task"
    | "model"
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

// A mature task that was skipped this run has no detail page in THIS run, but it
// did run earlier (run B). Rather than silently navigating away — a normal-looking
// row that jumps to a *different* run is a surprise — the id is a button that
// opens a small popover: it names the run the task last executed in, explains the
// carry-forward, and offers an explicit link there. Click-outside or Escape
// dismisses. The card is portaled to <body> with fixed positioning: anchoring it
// inside the cell would clip it, since the first column is a sticky stacking
// context that later rows paint over and the table scroll-box hides overflow.
function MatureTaskLink({
    t,
    sourceRun,
    className,
}: {
    t: TaskResultSummary;
    sourceRun: string;
    className: string;
}) {
    const [open, setOpen] = useState(false);
    const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
    const btnRef = useRef<HTMLButtonElement>(null);
    const popRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (!open) return;
        const place = () => {
            const r = btnRef.current?.getBoundingClientRect();
            if (r) setPos({ top: r.bottom + 6, left: r.left });
        };
        place();
        const onDown = (e: MouseEvent) => {
            const tgt = e.target as Node;
            if (
                !btnRef.current?.contains(tgt) &&
                !popRef.current?.contains(tgt)
            )
                setOpen(false);
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") setOpen(false);
        };
        // The card is viewport-fixed, so re-place on resize and just dismiss on
        // scroll rather than chase the trigger across the scrolling table.
        const onScroll = () => setOpen(false);
        document.addEventListener("mousedown", onDown);
        document.addEventListener("keydown", onKey);
        window.addEventListener("resize", place);
        window.addEventListener("scroll", onScroll, true);
        return () => {
            document.removeEventListener("mousedown", onDown);
            document.removeEventListener("keydown", onKey);
            window.removeEventListener("resize", place);
            window.removeEventListener("scroll", onScroll, true);
        };
    }, [open]);

    const sourceLabel = fmtRunTime(sourceRun);
    return (
        <>
            <button
                ref={btnRef}
                type="button"
                aria-haspopup="dialog"
                aria-expanded={open}
                title={matureLinkTooltip(sourceLabel)}
                onClick={() => setOpen((o) => !o)}
                className={`${className} cursor-help text-left`}
            >
                {humanizeTaskId(t.taskId)}
                <span
                    aria-hidden
                    className="ml-0.5 align-baseline text-gray-400 font-normal"
                >
                    ↗
                </span>
            </button>
            {open &&
                pos &&
                typeof document !== "undefined" &&
                createPortal(
                    <div
                        ref={popRef}
                        role="dialog"
                        aria-label="Mature task"
                        style={{ top: pos.top, left: pos.left }}
                        className="fixed z-50 w-64 rounded-md border border-gray-200 bg-white p-3 text-left text-xs font-normal leading-snug text-gray-600 shadow-lg"
                    >
                        <div className="font-semibold text-green-700">
                            Mature — skipped this run
                        </div>
                        <p className="mt-1">
                            Carried forward as a pass to save cost; it
                            wasn&apos;t executed here. It last actually ran in
                            run {sourceLabel}.
                        </p>
                        <Link
                            href={`/runs/${sourceRun}/${t.taskId}`}
                            onClick={() => setOpen(false)}
                            className="mt-2 inline-flex items-center gap-1 font-medium text-studio-blue hover:underline"
                        >
                            Open that execution
                            <span aria-hidden>↗</span>
                        </Link>
                    </div>,
                    document.body,
                )}
        </>
    );
}

// Task-id cell. Mature-skipped rows open the MatureTaskLink popover when an
// earlier execution is known; otherwise they fall back to a non-clickable span.
// Normal rows link straight to their in-run detail. Shared by the desktop table
// and the mobile cards.
function TaskIdCell({
    runId,
    t,
    className,
    matureSourceRuns,
    replicateCount = 1,
    replicatePassCount = 0,
}: {
    runId: string;
    t: TaskResultSummary;
    className: string;
    matureSourceRuns?: Record<string, string>;
    // How many replicates (repeated runs) of this task exist in the run. When
    // >1 the row collapses to a single entry with a k/N ✓ badge; the per-run
    // detail is reachable from the task page's run selector (?r=NN).
    replicateCount?: number;
    // How many of those replicates passed — shown as k/N and color-coded.
    replicatePassCount?: number;
}) {
    if (t.matureSkipped) {
        const sourceRun = matureSourceRuns?.[t.taskId];
        if (sourceRun) {
            return (
                <MatureTaskLink
                    t={t}
                    sourceRun={sourceRun}
                    className={className}
                />
            );
        }
        return (
            <span
                title={MATURE_NO_SOURCE_TOOLTIP}
                className={`${className} cursor-not-allowed text-gray-400`}
            >
                {humanizeTaskId(t.taskId)}
            </span>
        );
    }
    // For a collapsed replicate row, link to the SAME replicate the row's
    // status/score/cost describe (the representative), not implicitly to
    // replicate 0 — so clicking a green "Passed" row lands on the passing run.
    // In a multi-model run several variants share the taskId, so carry the
    // variant (?v=) too — otherwise every model's row would open the first
    // variant's detail. "default" is the implicit fallback, so it's omitted.
    const params = new URLSearchParams();
    if (replicateCount > 1) params.set("r", String(t.replicateIndex ?? 0));
    if (t.variant && t.variant !== DEFAULT_VARIANT) params.set("v", t.variant);
    const qs = params.toString();
    const href = `/runs/${runId}/${t.taskId}${qs ? `?${qs}` : ""}`;
    return (
        <Link href={href} className={className}>
            {humanizeTaskId(t.taskId)}
            {replicateCount > 1 && (
                <span
                    className={`ml-1.5 rounded border px-1 py-0.5 text-[10px] font-medium tabular-nums ${replicateBadgeClass(replicatePassCount, replicateCount)}`}
                    title={`${replicatePassCount} of ${replicateCount} replicates passed — open to switch between them`}
                >
                    {replicatePassCount}/{replicateCount} ✓
                </span>
            )}
        </Link>
    );
}

// Colour tier for the k/N ✓ replicate badge: all passed → green, some → amber,
// none → red. Kept as a named helper so the all/some/none intent is explicit
// and the JSX stays flat.
function replicateBadgeClass(passCount: number, total: number): string {
    if (passCount === total) return "bg-green-50 border-green-200 text-green-700";
    if (passCount > 0) return "bg-amber-50 border-amber-200 text-amber-700";
    return "bg-red-50 border-red-200 text-red-700";
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
    model: "asc",
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
        case "model":
            // The arm column sorts by whichever label it shows (model when
            // models differ, else the variant/arm id).
            return (a.model ?? a.variant ?? "").localeCompare(
                b.model ?? b.variant ?? "",
            );
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
    { key: "model", header: "Model" },
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
    matureSourceRuns,
}: {
    runId: string;
    tasks: TaskResultSummary[];
    selectedSet?: Set<string>;
    onToggleTag?: (tag: string) => void;
    emptyHint?: string;
    reviewsByTask?: Map<string, ReviewIndexEntry>;
    reviewSelectedSet?: Set<string>;
    onToggleReviewTag?: (tag: string) => void;
    // taskId → earlier run where a mature-skipped task last executed (run B). A
    // mature row with an entry links out; one without stays non-clickable.
    matureSourceRuns?: Record<string, string>;
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

    // How many rows share each (task, variant) — i.e. the replicate count for
    // that task under one model. Drives whether a row shows its replicate badge
    // + ?r link (only when >1, so single-run tasks aren't cluttered with a "#0").
    // Keyed by taskGroupKey so a multi-model run counts each model's replicates
    // separately rather than lumping all variants' rows together.
    const replicateCounts = useMemo(() => {
        const m = new Map<string, number>();
        for (const t of tasks) {
            const k = taskGroupKey(t);
            m.set(k, (m.get(k) ?? 0) + 1);
        }
        return m;
    }, [tasks]);

    // How many replicates of each task PASSED — drives the k/N ✓ badge so you
    // can see the pass ratio at a glance (green = all, amber = some, red = none).
    // Uses the shared per-task rollup so the badge, the collapse, and the run
    // page's pass-rate tile all apply the same "any replicate passed" rule.
    const replicatePassCounts = useMemo(() => perTaskPassCounts(tasks), [tasks]);

    // Collapse replicates to one row per task: repeated runs share a taskId, so
    // the grid shows a single entry with a k/N ✓ badge; the per-run detail is
    // selectable on the task page. The status pill and detail link come from a
    // representative replicate (a passing one when any passed, else the
    // lowest-index one), while the quantitative columns (score, duration, cost,
    // turns, tokens) are averaged across all repeats — see collapseReplicates.
    // Collapse BEFORE sorting so a metric-sorted view still shows one row per task.
    const collapsed = useMemo(() => collapseReplicates(tasks), [tasks]);

    // Show the arm column only when a run actually has multiple ARMS — rows split
    // on (taskId, variant), so gate on distinct VARIANT, not distinct model: a
    // same-model A/B (skill on/off, terse/detailed) varies only the variant, and
    // gating on model would render N identical unlabeled rows per task. Also show
    // it if models differ (a genuine multi-model run). A single-config run has
    // one arm across every row, so the column stays hidden (pure noise). When
    // shown, prefer the model label if models differ (the informative axis), else
    // the variant id (the arm name). The detail page always names the model too.
    const { showArm, armByModel } = useMemo(() => {
        const models = new Set(collapsed.map((t) => t.model).filter(Boolean));
        const variants = new Set(
            collapsed.map((t) => t.variant ?? DEFAULT_VARIANT),
        );
        return {
            showArm: models.size > 1 || variants.size > 1,
            armByModel: models.size > 1,
        };
    }, [collapsed]);
    // The label shown in the arm column / mobile card for one row.
    const armLabel = (t: TaskResultSummary): string =>
        (armByModel ? t.model ?? t.variant : t.variant) ?? DEFAULT_VARIANT;

    const sorted = useMemo(() => {
        const arr = [...collapsed];
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
    }, [collapsed, sort]);

    const onSort = (key: SortKey) => {
        setSort((cur) =>
            cur?.key === key
                ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
                : { key, dir: DEFAULT_DIR[key] },
        );
    };

    const visibleColumns = COLUMNS.filter(
        (c) =>
            (showTokens || !TOKEN_KEYS.has(c.key)) &&
            (showArm || c.key !== "model"),
    );
    // The arm column's header reads "Model" when models distinguish the arms,
    // else "Variant" (same-model A/B). The SortKey stays "model".
    const columnHeader = (col: (typeof COLUMNS)[number]): string =>
        col.key === "model" ? (armByModel ? "Model" : "Variant") : col.header;

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
                                            {columnHeader(col)}
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
                                                    aria-label={`What is ${columnHeader(col)}?`}
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
                        const review = reviewsByTask?.get(taskGroupKey(t));
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
                            key={`${taskGroupKey(t)}#${t.replicateIndex ?? 0}`}
                            className="group border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                        >
                            <td className="py-3 px-4 text-gray-700 sticky left-0 z-10 bg-white group-hover:bg-gray-50">
                                <div className="flex flex-col min-w-0 gap-0.5">
                                    <TaskIdCell
                                        runId={runId}
                                        t={t}
                                        className="text-gray-900 hover:text-studio-blue font-semibold"
                                        matureSourceRuns={matureSourceRuns}
                                        replicateCount={
                                            replicateCounts.get(
                                                taskGroupKey(t),
                                            ) ?? 1
                                        }
                                        replicatePassCount={
                                            replicatePassCounts.get(
                                                taskGroupKey(t),
                                            ) ?? 0
                                        }
                                    />
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
                            {showArm && (
                                <td
                                    className="py-3 px-4 text-gray-700 font-mono text-xs max-w-[16rem] truncate"
                                    title={
                                        t.model
                                            ? `model ${t.model} · variant ${t.variant ?? DEFAULT_VARIANT}`
                                            : `variant ${t.variant ?? DEFAULT_VARIANT}`
                                    }
                                >
                                    {armLabel(t)}
                                </td>
                            )}
                            <td className="py-3 px-4">
                                {t.matureSkipped ? (
                                    <MaturePill />
                                ) : (
                                    <StatusPill status={t.status} relabel />
                                )}
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
                    const review = reviewsByTask?.get(taskGroupKey(t));
                    const turnsTint = tintForRatio(
                        turnRatio(
                            displayedTurns(t.actualCommands, t.hasFinalReply),
                            t.expectedTurns,
                        ),
                    );
                    return (
                        <div
                            key={`${taskGroupKey(t)}#${t.replicateIndex ?? 0}`}
                            className="rounded-lg border border-gray-200 bg-white p-3 space-y-2"
                        >
                            <div className="flex items-start justify-between gap-2">
                                <TaskIdCell
                                    runId={runId}
                                    t={t}
                                    className="min-w-0 break-words font-semibold text-gray-900 hover:text-studio-blue"
                                    matureSourceRuns={matureSourceRuns}
                                    replicateCount={
                                        replicateCounts.get(taskGroupKey(t)) ?? 1
                                    }
                                    replicatePassCount={
                                        replicatePassCounts.get(
                                            taskGroupKey(t),
                                        ) ?? 0
                                    }
                                />
                                <span className="shrink-0">
                                    {t.matureSkipped ? (
                                        <MaturePill />
                                    ) : (
                                        <StatusPill status={t.status} relabel />
                                    )}
                                </span>
                            </div>
                            {showArm && (
                                <div
                                    className="font-mono text-[11px] text-gray-500 truncate"
                                    title={
                                        t.model
                                            ? `model ${t.model} · variant ${t.variant ?? DEFAULT_VARIANT}`
                                            : `variant ${t.variant ?? DEFAULT_VARIANT}`
                                    }
                                >
                                    {armLabel(t)}
                                </div>
                            )}
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
