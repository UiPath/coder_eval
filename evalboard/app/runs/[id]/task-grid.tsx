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
import { isPassStatus, perTaskPassCounts, statusSortRank } from "@/lib/status";
import { DEFAULT_VARIANT_ID, taskVariantKey, variantsOf } from "@/lib/variants";
import {
    displayedTurns,
    fmtTurnsCount,
    tintForRatio,
    turnRatio,
    turnsCellClasses,
} from "@/lib/turns";
import {
    expectedTimeTitle,
    fmtTimeRatioCell,
    timeCellClasses,
    timeRatio,
    tintForTimeRatio,
} from "@/lib/timing";
import { ChipButton } from "./chips";
import { withSource } from "@/app/_lib/source-param";
import { TableScroll } from "@/app/_components/scroll-table";
import { TOKEN_COLUMN_HELP } from "@/app/_components/col-help";

type SortKey =
    | "task"
    | "variant"
    | "status"
    | "score"
    | "duration"
    | "vsExp"
    | "cost"
    | "turns"
    | "input"
    | "output"
    | "cw"
    | "cr";

// Header tooltips. Token-column copy is shared with the message timeline via
// TOKEN_COLUMN_HELP; the rest is grid-specific.
const COLUMN_HELP: Partial<Record<SortKey, string>> = {
    ...TOKEN_COLUMN_HELP,
    turns: "Visible turns: one per tool call plus one for the final reply. Tinted against the task's hand-written expected_turns budget (yellow past 1.25×, red past 1.5×); untinted when the task declares none.",
    vsExp: "Duration ÷ the time this task is expected to need. The expected time is derived per task, per harness by the eval runner (its fastest passing run, or p10 once there are ten) and stamped into the run — never hand-written. Past 2× counts as slow; a task its harness has never passed shows —.",
    cost: "Total billed cost for this task, reported by the SDK (summed across turns).",
    variant: "Experiment arm this row was produced by. A run declaring `variants:` executes every task once per arm and keeps each arm's output in its own subtree, so the same task appears once per arm and the two rows are separate measurements — never collapsed together.",
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
    sourceId,
}: {
    t: TaskResultSummary;
    sourceRun: string;
    className: string;
    sourceId: string;
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
                            href={withSource(
                                `/runs/${sourceRun}/${t.taskId}`,
                                sourceId,
                            )}
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
    sourceId,
}: {
    runId: string;
    t: TaskResultSummary;
    className: string;
    matureSourceRuns?: Record<string, string>;
    sourceId: string;
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
                    sourceId={sourceId}
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
    //
    // On a multi-variant run the arm is part of the row's identity, so it has to
    // travel in the link too; without ?v= both arms of a task would open the
    // same (first-matching) transcript. A default-arm row omits the param, so
    // every link on an ordinary run is unchanged.
    const variant = t.variantId ?? DEFAULT_VARIANT_ID;
    const params = new URLSearchParams();
    if (replicateCount > 1) params.set("r", String(t.replicateIndex ?? 0));
    if (variant !== DEFAULT_VARIANT_ID) params.set("v", variant);
    const qs = params.toString();
    const href = withSource(
        `/runs/${runId}/${t.taskId}${qs ? `?${qs}` : ""}`,
        sourceId,
    );
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

// One neutral style, not a per-arm colour: the id is already the signal, and it
// leaves colour in this column meaning pass/fail (the replicate badge).
function VariantChip({ variantId }: { variantId: string }) {
    return (
        <span className="inline-block rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 font-mono text-[11px] text-gray-600 whitespace-nowrap">
            {variantId}
        </span>
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
    status: "asc",
    score: "desc",
    duration: "desc",
    vsExp: "desc",
    cost: "desc",
    turns: "desc",
    input: "desc",
    output: "desc",
    cw: "desc",
    cr: "desc",
    variant: "asc",
};

// Final tiebreak for both sort paths — without the variant leg, two rows of one
// task have no defined order and reshuffle between renders.
function byTaskThenVariant(a: TaskResultSummary, b: TaskResultSummary): number {
    return (
        a.taskId.localeCompare(b.taskId) ||
        (a.variantId ?? DEFAULT_VARIANT_ID).localeCompare(
            b.variantId ?? DEFAULT_VARIANT_ID,
        )
    );
}

function compare(
    a: TaskResultSummary,
    b: TaskResultSummary,
    key: SortKey,
): number {
    switch (key) {
        case "task":
            return a.taskId.localeCompare(b.taskId);
        case "variant":
            return (a.variantId ?? DEFAULT_VARIANT_ID).localeCompare(
                b.variantId ?? DEFAULT_VARIANT_ID,
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
        case "vsExp":
            return (
                (timeRatio(a.durationSeconds, a.expectedSeconds) ?? -Infinity) -
                (timeRatio(b.durationSeconds, b.expectedSeconds) ?? -Infinity)
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
    // Only rendered when the run has more than one arm — see visibleColumns.
    { key: "variant", header: "Variant" },
    { key: "status", header: "Status" },
    { key: "score", header: "Score", align: "right" },
    { key: "duration", header: "Duration", align: "right" },
    { key: "vsExp", header: "vs Expected", align: "right" },
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
    sub,
    subClass = "text-gray-500",
    title,
}: {
    label: string;
    value: string;
    valueClass?: string;
    // Second line under the value, e.g. a duration's ratio to its expected time.
    sub?: string;
    subClass?: string;
    // Hover text for the value, e.g. what a tinted ratio was measured against.
    title?: string;
}) {
    return (
        <div className="min-w-0" title={title}>
            <div className="text-[10px] uppercase tracking-wide text-gray-400">
                {label}
            </div>
            <div className={`tabular-nums ${valueClass}`}>{value}</div>
            {sub && (
                <div className={`text-[10px] tabular-nums ${subClass}`}>
                    {sub}
                </div>
            )}
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
    sourceId,
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
    // Container this run came from; carried on every row link. Mature-source
    // runs are resolved within the same source, so they carry it too.
    sourceId: string;
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

    // Experiment arms present in this run. One entry (or none) on an ordinary
    // run, in which case every variant affordance below stays hidden and the
    // grid renders exactly as it did before variants were readable.
    const variantIds = useMemo(() => variantsOf(tasks), [tasks]);
    const hasVariants = variantIds.length > 1;

    // How many rows share each (variant, task) — i.e. the replicate count for
    // that arm of that task. Drives whether a row shows its replicate badge +
    // ?r link (only when >1, so single-run tasks aren't cluttered with a "#0").
    const replicateCounts = useMemo(() => {
        const m = new Map<string, number>();
        for (const t of tasks)
            m.set(taskVariantKey(t), (m.get(taskVariantKey(t)) ?? 0) + 1);
        return m;
    }, [tasks]);

    // How many replicates of each task PASSED — drives the k/N ✓ badge so you
    // can see the pass ratio at a glance (green = all, amber = some, red = none).
    // Uses the shared per-task rollup so the badge, the collapse, and the run
    // page's pass-rate tile all apply the same "any replicate passed" rule.
    const replicatePassCounts = useMemo(() => perTaskPassCounts(tasks), [tasks]);

    // Collapse replicates to one row per (variant, task): repeated runs share a
    // taskId, so the grid shows a single entry with a k/N ✓ badge; the per-run
    // detail is selectable on the task page. The representative is chosen so its
    // status, score, cost, duration AND detail link all describe the SAME run:
    // prefer a passing replicate when any passed (else the lowest-index one),
    // breaking ties by lowest replicateIndex for stability. Pick BEFORE sorting
    // so a metric-sorted view still shows one row per task.
    //
    // Replicates collapse; ARMS DO NOT. Two variants of one task are separate
    // measurements of separate configurations — collapsing them would let a pass
    // in one arm hide a failure in the other, which is the whole signal an A/B
    // run exists to show.
    const collapsed = useMemo(() => {
        const byTask = new Map<string, TaskResultSummary>();
        for (const t of tasks) {
            const key = taskVariantKey(t);
            const cur = byTask.get(key);
            if (!cur) {
                byTask.set(key, t);
                continue;
            }
            const curPass = isPassStatus(cur.status);
            const tPass = isPassStatus(t.status);
            if (curPass !== tPass) {
                // A passing replicate always wins over a non-passing one.
                if (tPass) byTask.set(key, t);
            } else if ((t.replicateIndex ?? 0) < (cur.replicateIndex ?? 0)) {
                byTask.set(key, t);
            }
        }
        return [...byTask.values()];
    }, [tasks]);

    const sorted = useMemo(() => {
        const arr = [...collapsed];
        if (sort) {
            arr.sort((a, b) => {
                const c = compare(a, b, sort.key);
                if (c !== 0) return sort.dir === "asc" ? c : -c;
                return byTaskThenVariant(a, b);
            });
        } else {
            // Failures first, then task id — but ranked on the TASK by its worst
            // arm. Ranking rows independently splits exactly the pairs worth
            // reading: a task whose arms disagree gets one row at the top of the
            // grid and the other at the bottom. Unchanged without variants, where
            // a task's single row IS its worst arm.
            const worstByTask = new Map<string, number>();
            for (const t of arr) {
                const r = statusSortRank(t.status);
                const cur = worstByTask.get(t.taskId);
                if (cur === undefined || r < cur) worstByTask.set(t.taskId, r);
            }
            arr.sort(
                (a, b) =>
                    (worstByTask.get(a.taskId) ?? 0) -
                        (worstByTask.get(b.taskId) ?? 0) ||
                    byTaskThenVariant(a, b),
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

    // The Variant column only carries information on a run that actually has more
    // than one arm; on every ordinary run it would be a column of identical
    // "default" cells, so it is dropped and the grid renders as it always has.
    const visibleColumns = COLUMNS.filter(
        (c) =>
            (showTokens || !TOKEN_KEYS.has(c.key)) &&
            (hasVariants || c.key !== "variant"),
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
                            return (
                                <th
                                    key={col.key}
                                    aria-sort={ariaSort}
                                    title={COLUMN_HELP[col.key]}
                                    // nowrap: a narrow column sizes to its cells,
                                    // which wraps a two-word header onto two lines.
                                    className={`py-3 px-4 font-medium whitespace-nowrap ${alignCls} ${stickyCls}`}
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
                                </th>
                            );
                        })}
                    </tr>
                </thead>
                <tbody>
                    {sorted.map((t) => {
                        const review = reviewsByTask?.get(t.taskId);
                        // Two efficiency signals, side by side while the
                        // wall-clock one is being watched. Seconds live in the
                        // vs Expected cell rather than on Duration: a long task
                        // is not a slow one.
                        const timeRatioValue = timeRatio(
                            t.durationSeconds,
                            t.expectedSeconds,
                        );
                        const timeTint = tintForTimeRatio(timeRatioValue);
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
                            key={`${taskVariantKey(t)}#${t.replicateIndex ?? 0}`}
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
                                            replicateCounts.get(taskVariantKey(t)) ?? 1
                                        }
                                        replicatePassCount={
                                            replicatePassCounts.get(taskVariantKey(t)) ?? 0
                                        }
                                        sourceId={sourceId}
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
                            {hasVariants && (
                                <td className="py-3 px-4">
                                    <VariantChip
                                        variantId={
                                            t.variantId ?? DEFAULT_VARIANT_ID
                                        }
                                    />
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
                            <td
                                className={`py-3 px-4 text-right tabular-nums font-medium ${timeCellClasses(timeTint)}`}
                                title={expectedTimeTitle(t.expectedSeconds)}
                            >
                                {fmtTimeRatioCell(timeRatioValue)}
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
                    const timeRatioValue = timeRatio(
                        t.durationSeconds,
                        t.expectedSeconds,
                    );
                    const timeTint = tintForTimeRatio(timeRatioValue);
                    const turnsTint = tintForRatio(
                        turnRatio(
                            displayedTurns(t.actualCommands, t.hasFinalReply),
                            t.expectedTurns,
                        ),
                    );
                    return (
                        <div
                            key={`${taskVariantKey(t)}#${t.replicateIndex ?? 0}`}
                            className="rounded-lg border border-gray-200 bg-white p-3 space-y-2"
                        >
                            <div className="flex items-start justify-between gap-2">
                                <TaskIdCell
                                    runId={runId}
                                    t={t}
                                    className="min-w-0 break-words font-semibold text-gray-900 hover:text-studio-blue"
                                    matureSourceRuns={matureSourceRuns}
                                    replicateCount={
                                        replicateCounts.get(taskVariantKey(t)) ?? 1
                                    }
                                        replicatePassCount={
                                            replicatePassCounts.get(taskVariantKey(t)) ?? 0
                                        }
                                    sourceId={sourceId}
                                />
                                <span className="shrink-0 flex items-center gap-1.5">
                                    {hasVariants && (
                                        <VariantChip
                                            variantId={
                                                t.variantId ??
                                                DEFAULT_VARIANT_ID
                                            }
                                        />
                                    )}
                                    {t.matureSkipped ? (
                                        <MaturePill />
                                    ) : (
                                        <StatusPill status={t.status} relabel />
                                    )}
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
                                    sub={
                                        timeRatioValue != null
                                            ? fmtTimeRatioCell(timeRatioValue)
                                            : undefined
                                    }
                                    subClass={timeCellClasses(timeTint)}
                                    title={expectedTimeTitle(t.expectedSeconds)}
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
