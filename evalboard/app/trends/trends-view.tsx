"use client";

import Link from "next/link";
import { useMemo, useState, useTransition } from "react";
import {
    type TaskTrend,
    type TaskHistoryEntry,
} from "@/lib/trends";
import type { TagCount } from "@/lib/overview";
import { fmtRunTime, fmtDuration, humanizeTaskId } from "@/lib/format";
import {
    displayedTurns,
    fmtTurnsCount,
    tintForRatio,
    turnRatio,
    turnsCellClasses,
} from "@/lib/turns";
import { MATURE_TOOLTIP } from "@/lib/pills";
import { ChipLegend, MergedTagRail } from "@/app/_overview/tag-rail";
import { ChipButton } from "@/app/runs/[id]/chips";
import { TableScroll } from "@/app/_components/scroll-table";
import { CollapsibleRail } from "@/app/_components/collapsible-rail";
import { VersionChip } from "@/app/_components/version-list";
import { fetchTaskHistoryAction } from "./actions";

function fmtUsd(c: number | null): string {
    if (c == null) return "—";
    if (c < 0.01) return `$${c.toFixed(3)}`;
    return `$${c.toFixed(2)}`;
}

function fmtPct(p: number): string {
    return `${Math.round(p * 100)}%`;
}

function fmtScore(s: number | null): string {
    if (s == null) return "—";
    return s.toFixed(2);
}

function fmtCount(n: number | null): string {
    if (n == null) return "—";
    return n.toFixed(0);
}

function passRateClass(rate: number, hasRuns: boolean): string {
    if (!hasRuns) return "text-gray-500";
    if (rate >= 1.0) return "text-green-700";
    return "text-red-700";
}

type SortKey =
    | "task"
    | "runs"
    | "passRate"
    | "avgDuration"
    | "avgCost"
    | "avgTurns";
type SortDir = "asc" | "desc";

// First-click direction per column — pass rate asc is the "worst first" view
// users expect, and runs/duration/cost/tools/turns are most informative high-to-low.
const DEFAULT_DIR: Record<SortKey, SortDir> = {
    task: "asc",
    runs: "desc",
    passRate: "asc",
    avgDuration: "desc",
    avgCost: "desc",
    avgTurns: "desc",
};

function cmpNullable(
    a: number | null,
    b: number | null,
    dir: SortDir,
): number {
    // Nulls always last so failed-only tasks (no SUCCESS avg) don't surface
    // when sorting cost/duration/turns desc.
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return dir === "asc" ? a - b : b - a;
}

function sortTasks(
    tasks: TaskTrend[],
    key: SortKey,
    dir: SortDir,
): TaskTrend[] {
    const arr = [...tasks];
    arr.sort((a, b) => {
        let v = 0;
        switch (key) {
            case "task": {
                const an = humanizeTaskId(a.taskId);
                const bn = humanizeTaskId(b.taskId);
                v = an.localeCompare(bn);
                if (dir === "desc") v = -v;
                break;
            }
            case "runs":
                v =
                    dir === "asc"
                        ? a.totalRuns - b.totalRuns
                        : b.totalRuns - a.totalRuns;
                break;
            case "passRate":
                v =
                    dir === "asc"
                        ? a.passRate - b.passRate
                        : b.passRate - a.passRate;
                break;
            case "avgDuration":
                v = cmpNullable(
                    a.avgDurationSeconds,
                    b.avgDurationSeconds,
                    dir,
                );
                break;
            case "avgCost":
                v = cmpNullable(a.avgCostUsd, b.avgCostUsd, dir);
                break;
            case "avgTurns":
                v = cmpNullable(
                    a.avgTotalTurns ?? a.avgActualCommands,
                    b.avgTotalTurns ?? b.avgActualCommands,
                    dir,
                );
                break;
        }
        if (v !== 0) return v;
        // Tiebreaker: more-run tasks float to the natural edge of their group
        // (chronic 0% over many runs above one-shot 0%; mature 100% above
        // single-run 100%). Then taskId for determinism.
        if (key !== "runs" && a.totalRuns !== b.totalRuns) {
            return b.totalRuns - a.totalRuns;
        }
        return a.taskId.localeCompare(b.taskId);
    });
    return arr;
}

function statusFill(status: string | null): string {
    if (status == null) return "bg-gray-300";
    if (status === "SUCCESS") return "bg-green-500";
    // All non-success outcomes share one red — the trends view treats every
    // failure mode as equally bad rather than ranking FAILED vs ERROR.
    return "bg-red-500";
}

function StatusBar({
    runIds,
    statuses,
}: {
    // Newest-first run axis shared by every row (TrendsData.runIds). Slots
    // the per-task statuses onto the common timeline so runs the task is
    // missing from show as explicit gaps instead of silently compressing —
    // without this, a renamed/retired task's strip ends flush-right and is
    // indistinguishable from one that ran in the newest run.
    runIds: string[];
    statuses: { runId: string; status: string | null; matureSkipped?: boolean }[];
}) {
    if (statuses.length === 0) {
        // Unreachable via aggregate() — every emitted trend carries ≥1
        // status. Kept as a deliberate defensive no-op for directly-built
        // TaskTrends.
        return <span className="text-xs text-gray-400">—</span>;
    }
    const byRun = new Map(statuses.map((s) => [s.runId, s]));
    // Oldest-to-newest left-to-right so the rightmost bar is the most
    // recent run — matches the timeline convention on the front page.
    const ordered = [...runIds].reverse();
    return (
        <div className="flex items-end gap-[2px] h-4">
            {ordered.map((runId) => {
                const entry = byRun.get(runId);
                if (!entry) {
                    return (
                        <span
                            key={runId}
                            title={`${runId} · not in run`}
                            className="w-[6px] h-[5px] rounded-sm bg-gray-50 border border-gray-300"
                        />
                    );
                }
                // A mature-skipped run is a pass that wasn't executed — draw it
                // as a hollow green stub so it's distinct from a real pass while
                // still reading as green (not a failure).
                if (entry.matureSkipped) {
                    return (
                        <span
                            key={runId}
                            title={`${runId} · mature (skipped, carried forward)`}
                            className="w-[6px] h-full rounded-sm bg-green-100 border border-green-400"
                        />
                    );
                }
                return (
                    <span
                        key={runId}
                        title={`${runId} · ${entry.status ?? "unknown"}`}
                        className={`w-[6px] h-full rounded-sm ${statusFill(entry.status)}`}
                    />
                );
            })}
        </div>
    );
}

function HistoryTable({
    taskId,
    entries,
}: {
    taskId: string;
    entries: TaskHistoryEntry[];
}) {
    if (entries.length === 0) {
        return (
            <span className="text-xs text-gray-500">
                No history for this task in the recent runs.
            </span>
        );
    }
    return (
        <div className="overflow-x-auto">
            <table className="w-full text-xs">
                <thead>
                    <tr className="text-left text-gray-500 border-b border-gray-200">
                        <th className="py-1 pr-3 font-medium">Run</th>
                        <th className="py-1 pr-3 font-medium">Status</th>
                        <th className="py-1 pr-3 font-medium text-right">
                            Duration
                        </th>
                        <th className="py-1 pr-3 font-medium text-right">
                            Cost
                        </th>
                        <th className="py-1 pr-3 font-medium text-right">
                            Turns
                        </th>
                        <th className="py-1 pr-3 font-medium text-right">
                            Score
                        </th>
                        <th className="py-1 pr-3 font-medium">Commits</th>
                        <th className="py-1 font-medium">Tags</th>
                    </tr>
                </thead>
                <tbody>
                    {entries.map((e) => (
                        <tr
                            key={e.runId}
                            className="border-b border-gray-100 last:border-0"
                        >
                            <td className="py-1 pr-3">
                                {e.matureSkipped ? (
                                    // Skipped this run — no per-task detail to
                                    // open, so the date isn't a (dead) link.
                                    <span
                                        className="font-mono text-gray-400 cursor-not-allowed"
                                        title={MATURE_TOOLTIP}
                                    >
                                        {fmtRunTime(e.runId)}
                                    </span>
                                ) : (
                                    <Link
                                        href={`/runs/${e.runId}/${taskId}`}
                                        className="font-mono text-studio-blue hover:underline"
                                    >
                                        {fmtRunTime(e.runId)}
                                    </Link>
                                )}
                            </td>
                            <td className="py-1 pr-3">
                                <span
                                    className={`inline-block w-2 h-2 rounded-full align-middle mr-1.5 ${statusFill(e.status)}`}
                                />
                                <span className="text-gray-700">
                                    {e.matureSkipped
                                        ? "Mature"
                                        : (e.status ?? "—")}
                                </span>
                            </td>
                            <td className="py-1 pr-3 text-right tabular-nums text-gray-700">
                                {e.matureSkipped
                                    ? "—"
                                    : fmtDuration(e.durationSeconds)}
                            </td>
                            <td className="py-1 pr-3 text-right tabular-nums text-gray-700">
                                {e.matureSkipped
                                    ? "—"
                                    : fmtUsd(e.totalCostUsd)}
                            </td>
                            {e.matureSkipped ? (
                                // Not executed — no turns to compare to budget.
                                <td
                                    className="py-1 pr-3 text-right tabular-nums text-gray-400"
                                    title={MATURE_TOOLTIP}
                                >
                                    —
                                </td>
                            ) : (
                                (() => {
                                    const tint = tintForRatio(
                                        turnRatio(
                                            e.totalTurns,
                                            e.expectedTurns,
                                        ),
                                    );
                                    return (
                                        <td
                                            className={`py-1 pr-3 text-right tabular-nums font-medium ${turnsCellClasses(tint)}`}
                                            title={
                                                e.expectedTurns != null
                                                    ? `expected_turns target: ${e.expectedTurns}`
                                                    : "no expected_turns target set"
                                            }
                                        >
                                            {fmtTurnsCount(
                                                displayedTurns(
                                                    e.actualCommands,
                                                    e.hasFinalReply,
                                                ),
                                            )}
                                        </td>
                                    );
                                })()
                            )}
                            <td className="py-1 pr-3 text-right tabular-nums text-gray-700">
                                {e.matureSkipped
                                    ? "—"
                                    : fmtScore(e.weightedScore)}
                            </td>
                            <td className="py-1 pr-3 align-top">
                                {/* Recent runs stamp ~24 component versions
                                    (3 core + ~21 tool plugins). Show only the 3
                                    core versions per history row — the tool
                                    plugins are noise in this view. */}
                                <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-500 font-mono">
                                    {e.componentShas.slice(0, 3).map((c) => (
                                        <VersionChip key={c.name} {...c} />
                                    ))}
                                </span>
                            </td>
                            <td className="py-1">
                                {e.failureTags.length > 0 && (
                                    <span className="flex flex-wrap gap-1">
                                        {e.failureTags.map((t) => (
                                            <span
                                                key={t}
                                                className="text-[10px] leading-none px-1 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200"
                                            >
                                                {t}
                                            </span>
                                        ))}
                                    </span>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function TaskRow({
    t,
    runIds,
    expanded,
    history,
    pending,
    onToggle,
}: {
    t: TaskTrend;
    runIds: string[];
    expanded: boolean;
    history: TaskHistoryEntry[] | null;
    pending: boolean;
    onToggle: () => void;
}) {
    const rateClass = passRateClass(t.passRate, t.totalRuns > 0);
    return (
        <>
            <tr
                onClick={onToggle}
                className="group border-b border-gray-100 hover:bg-gray-50 cursor-pointer"
            >
                <td className="py-2 px-4 sticky left-0 z-10 bg-white group-hover:bg-gray-50">
                    <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="font-medium text-gray-900">
                            {humanizeTaskId(t.taskId)}
                        </span>
                        {(t.matureSkips ?? 0) > 0 && (
                            <span
                                title={`Mature: carried forward as a pass without running in ${t.matureSkips} of the last ${t.totalRuns} runs, to save cost.`}
                                className="text-[10px] font-medium text-green-700"
                            >
                                Mature
                            </span>
                        )}
                    </div>
                    {(t.skill ||
                        t.tags.length > 0 ||
                        t.dominantFailureTags.length > 0) && (
                        <div
                            className="flex flex-wrap gap-1 mt-0.5"
                            onClick={(e) => e.stopPropagation()}
                        >
                            {t.skill && (
                                <ChipButton
                                    key={`s:${t.skill}`}
                                    tag={t.skill}
                                    variant="skill"
                                    size="sm"
                                    active={false}
                                />
                            )}
                            {t.dominantFailureTags.map((tag) => (
                                <ChipButton
                                    key={`r:${tag.tag}`}
                                    tag={tag.tag}
                                    count={tag.count}
                                    variant="review"
                                    size="sm"
                                    active={false}
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
                                        active={false}
                                    />
                                ))}
                        </div>
                    )}
                </td>
                <td className="py-2 px-3 tabular-nums text-right text-gray-700">
                    {t.totalRuns}
                </td>
                <td
                    className={`py-2 px-3 tabular-nums text-right font-medium ${rateClass}`}
                >
                    {fmtPct(t.passRate)}
                </td>
                <td className="py-2 px-3">
                    <StatusBar runIds={runIds} statuses={t.recentStatuses} />
                </td>
                <td className="py-2 px-3 tabular-nums text-right text-gray-700">
                    {fmtDuration(t.avgDurationSeconds)}
                </td>
                <td className="py-2 px-3 tabular-nums text-right text-gray-700">
                    {fmtUsd(t.avgCostUsd)}
                </td>
                <td className="py-2 px-3 tabular-nums text-right text-gray-700">
                    {fmtCount(t.avgTotalTurns ?? t.avgActualCommands)}
                </td>
                <td className="py-2 px-2 text-xs text-gray-400">
                    {expanded ? "▾" : "▸"}
                </td>
            </tr>
            {expanded && (
                <tr className="bg-gray-50 border-b border-gray-100">
                    <td colSpan={8} className="px-4 py-3">
                        {pending && !history ? (
                            <span className="text-xs text-gray-500">
                                Loading…
                            </span>
                        ) : history == null ? (
                            <span className="text-xs text-gray-500">
                                No data.
                            </span>
                        ) : (
                            <HistoryTable taskId={t.taskId} entries={history} />
                        )}
                    </td>
                </tr>
            )}
        </>
    );
}

function SortableHeader({
    label,
    columnKey,
    activeKey,
    activeDir,
    onSort,
    align = "left",
    title,
    sticky = false,
}: {
    label: string;
    columnKey: SortKey;
    activeKey: SortKey;
    activeDir: SortDir;
    onSort: (key: SortKey) => void;
    align?: "left" | "right";
    title?: string;
    // Pin this header to the left edge so it stays visible while the metric
    // columns scroll horizontally. Pairs with the sticky first body cell.
    sticky?: boolean;
}) {
    const isActive = columnKey === activeKey;
    const arrow = isActive ? (activeDir === "asc" ? "▲" : "▼") : "";
    return (
        <th
            className={`py-2 px-3 font-medium cursor-pointer select-none hover:text-gray-900 ${align === "right" ? "text-right" : ""} ${isActive ? "text-gray-900" : ""} ${sticky ? "sticky left-0 z-10 bg-gray-50" : ""}`}
            title={title}
            onClick={() => onSort(columnKey)}
            aria-sort={
                isActive
                    ? activeDir === "asc"
                        ? "ascending"
                        : "descending"
                    : "none"
            }
        >
            <span className="inline-flex items-center gap-1">
                {label}
                <span className="text-[9px] text-gray-400 w-2 inline-block">
                    {arrow}
                </span>
            </span>
        </th>
    );
}

export function TrendsView({
    tasks,
    runIds,
    q,
    activeTag,
    skills,
    taskTags,
    reviewTags,
    provenance,
}: {
    tasks: TaskTrend[];
    // Newest-first run axis from TrendsData — see StatusBar.
    runIds: string[];
    q: string | null;
    activeTag: string | null;
    skills: TagCount[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
    provenance: { count: number; oldest: string; newest: string } | null;
}) {
    const [openTaskId, setOpenTaskId] = useState<string | null>(null);
    const [history, setHistory] = useState<Record<string, TaskHistoryEntry[]>>(
        {},
    );
    const [pending, startTransition] = useTransition();
    const [sortKey, setSortKey] = useState<SortKey>("passRate");
    const [sortDir, setSortDir] = useState<SortDir>(DEFAULT_DIR.passRate);

    function onSort(key: SortKey) {
        if (key === sortKey) {
            setSortDir((d) => (d === "asc" ? "desc" : "asc"));
        } else {
            setSortKey(key);
            setSortDir(DEFAULT_DIR[key]);
        }
    }

    const sortedTasks = useMemo(
        () => sortTasks(tasks, sortKey, sortDir),
        [tasks, sortKey, sortDir],
    );

    // Maturity tally over the tasks currently in view (respects the active
    // filter). A "mature" task is one the nightly skipped at least once in the
    // window. Surfaced as a terse `x/y (z%) mature` — the per-run detail lives
    // in the Recent strip's hollow-green slots, not in this line.
    const maturity = useMemo(() => {
        let matureTasks = 0;
        for (const t of tasks) {
            if ((t.matureSkips ?? 0) > 0) matureTasks += 1;
        }
        const pct = tasks.length > 0 ? matureTasks / tasks.length : 0;
        const label = `${matureTasks}/${tasks.length} (${fmtPct(pct)}) mature`;
        return { matureTasks, label };
    }, [tasks]);

    function toggle(taskId: string) {
        setOpenTaskId((cur) => (cur === taskId ? null : taskId));
        if (history[taskId] != null) return;
        startTransition(async () => {
            const entries = await fetchTaskHistoryAction(taskId);
            setHistory((prev) => ({ ...prev, [taskId]: entries }));
        });
    }

    const filterActive = !!q || !!activeTag;

    return (
        <div className="space-y-5">
            <div>
                <h1 className="text-xl font-semibold text-gray-900">
                    Task trends
                </h1>
                <p className="text-xs text-gray-500 mt-0.5">
                    Per-task pass rate and averages across recent runs.
                    Averages cover successful runs only.
                </p>
                {provenance && (
                    <p className="text-xs text-gray-500 mt-0.5 font-mono">
                        Last {provenance.count} runs · {provenance.oldest} →{" "}
                        {provenance.newest}
                    </p>
                )}
                {maturity.matureTasks > 0 && (
                    <p
                        className="text-xs font-medium text-green-700 mt-0.5"
                        title="Mature tasks were carried forward as passes without running, to save cost (the hollow-green slots in Recent)."
                    >
                        {maturity.label}
                    </p>
                )}
            </div>

            <section className="border border-gray-200 rounded-lg bg-white p-4 space-y-2">
                <ChipLegend />
                <CollapsibleRail id="trends-tagrail">
                    <MergedTagRail
                        skills={skills}
                        taskTags={taskTags}
                        reviewTags={reviewTags}
                        activeTag={activeTag}
                        basePath="/trends"
                        q={q}
                        limit={24}
                    />
                </CollapsibleRail>
                {q &&
                    skills.length === 0 &&
                    taskTags.length === 0 &&
                    reviewTags.length === 0 && (
                        <p className="text-xs text-gray-500">
                            No tags match{" "}
                            <span className="font-mono">{q}</span>.
                        </p>
                    )}
            </section>

            {tasks.length === 0 ? (
                <div className="bg-white border border-gray-200 rounded-lg p-6 text-center text-sm text-gray-500">
                    {filterActive ? (
                        <>No tasks match the current filter.</>
                    ) : (
                        <>No recent runs.</>
                    )}
                </div>
            ) : (
                <TableScroll>
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                <SortableHeader
                                    label="Task"
                                    columnKey="task"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    sticky
                                />
                                <SortableHeader
                                    label="Runs"
                                    columnKey="runs"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    align="right"
                                    title="Runs that include this task — fewer than the usable-run window when a task was added, renamed, or retired mid-window"
                                />
                                <SortableHeader
                                    label="Pass rate"
                                    columnKey="passRate"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    align="right"
                                />
                                <th
                                    className="py-2 px-3 font-medium"
                                    title="One slot per usable run in the window (unreadable/empty runs are excluded entirely), oldest → newest. Hollow gray stubs mark runs that don't include this task (added, renamed, or retired mid-window); hollow-green slots are mature runs skipped to save cost (carried forward as a pass)."
                                >
                                    Recent
                                </th>
                                <SortableHeader
                                    label="Avg duration"
                                    columnKey="avgDuration"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    align="right"
                                    title="Average over successful runs only"
                                />
                                <SortableHeader
                                    label="Avg cost"
                                    columnKey="avgCost"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    align="right"
                                    title="Average over successful runs only"
                                />
                                <SortableHeader
                                    label="Avg turns"
                                    columnKey="avgTurns"
                                    activeKey={sortKey}
                                    activeDir={sortDir}
                                    onSort={onSort}
                                    align="right"
                                    title="Average tool calls over successful runs only"
                                />
                                <th className="py-2 px-2"></th>
                            </tr>
                        </thead>
                        <tbody>
                            {sortedTasks.map((t) => (
                                <TaskRow
                                    key={t.taskId}
                                    t={t}
                                    runIds={runIds}
                                    expanded={openTaskId === t.taskId}
                                    history={history[t.taskId] ?? null}
                                    pending={
                                        pending && openTaskId === t.taskId
                                    }
                                    onToggle={() => toggle(t.taskId)}
                                />
                            ))}
                        </tbody>
                    </table>
                </TableScroll>
            )}
        </div>
    );
}
