import Link from "next/link";
import {
    getAdhocRunListing,
    getOverview,
    getRunListing,
    type TagCount,
} from "@/lib/overview";
import { fmtDuration, fmtRunTime } from "@/lib/format";
import { WindowSelector } from "./_components/window-selector";
import { WINDOWS, type Window } from "@/lib/reviews-types";
import { DailySuccessChart } from "./_overview/daily-chart";
import { TurnBudgetChart } from "./_overview/turn-budget-chart";
import { ChipLegend, MergedTagRail } from "./_overview/tag-rail";
import { TableScroll } from "./_components/scroll-table";
import { CollapsibleRail } from "./_components/collapsible-rail";

export const dynamic = "force-dynamic";

const DEFAULT_LIMIT = 20;
const ADHOC_LIMIT = 25;

function parseWindow(raw: string | string[] | undefined): Window {
    const v = Array.isArray(raw) ? raw[0] : raw;
    return WINDOWS.includes(v as Window) ? (v as Window) : "30d";
}

function parseTag(raw: string | string[] | undefined): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return null;
    const trimmed = v.trim();
    if (!trimmed || trimmed.length > 80) return null;
    // Charset matches what review.json emits today (word chars, plus
    // `.`, `:`, `/`, `+`, `-`). Don't tighten without auditing real tags.
    if (!/^[\w.:/+-]+$/.test(trimmed)) return null;
    return trimmed;
}

function parseQ(raw: string | string[] | undefined): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return null;
    const trimmed = v.trim();
    return trimmed ? trimmed.slice(0, 200) : null;
}

function parseLimit(raw: string | string[] | undefined): number | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (v === "all") return null;
    if (!v) return DEFAULT_LIMIT;
    const n = parseInt(v, 10);
    if (!Number.isFinite(n) || n <= 0) return DEFAULT_LIMIT;
    return Math.min(n, 10000);
}

function fmtCost(c: number | null): string {
    if (c == null) return "—";
    return `$${c.toFixed(2)}`;
}

function passClass(pct: number | null, hasTasks: boolean): string {
    if (!hasTasks || pct == null) return "text-gray-500";
    if (pct >= 80) return "text-green-700";
    if (pct >= 50) return "text-gray-700";
    return "text-red-700";
}

// Rail-level q filter: substring match on tag name only. This is narrower
// than getRunListing's q (which also matches taskId / humanized id) by
// design — rails are a tag namespace, the table is a task namespace.
function filterTagsByQuery(tags: TagCount[], q: string | null): TagCount[] {
    if (!q) return tags;
    const needle = q.toLowerCase();
    return tags.filter((t) => t.tag.toLowerCase().includes(needle));
}

function buildHref(
    params: Record<string, string | number | null | undefined>,
): string {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
        if (v == null || v === "") continue;
        sp.set(k, String(v));
    }
    const qs = sp.toString();
    return qs ? `/?${qs}` : "/";
}

export default async function Page({
    searchParams,
}: {
    searchParams: Promise<{
        window?: string;
        tag?: string;
        q?: string;
        limit?: string;
    }>;
}) {
    const params = await searchParams;
    const window = parseWindow(params.window);
    const activeTag = parseTag(params.tag);
    const q = parseQ(params.q);
    const limit = parseLimit(params.limit);
    const isFiltered = activeTag != null || q != null;

    const [overview, listing, adhocRows] = await Promise.all([
        getOverview(window, activeTag, q),
        getRunListing(window, activeTag, q, limit),
        getAdhocRunListing(ADHOC_LIMIT),
    ]);

    const skills = filterTagsByQuery(overview.skills, q);
    const taskTags = filterTagsByQuery(overview.taskTags, q);
    const reviewTags = filterTagsByQuery(overview.reviewTags, q);

    const shownCount = listing.rows.length;
    const matchedCount = listing.matchedCount;
    const totalInWindow = listing.totalInWindow;
    const tableTotalLabel = isFiltered ? matchedCount : totalInWindow;
    const hasMore = limit != null && shownCount < tableTotalLabel;

    const showMoreHref = buildHref({
        window,
        tag: activeTag,
        q,
        limit: Math.min(tableTotalLabel, shownCount + DEFAULT_LIMIT),
    });
    const showAllHref = buildHref({ window, tag: activeTag, q, limit: "all" });
    const clearAllHref = buildHref({ window });

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">
                    Recent runs
                </h1>
                <p className="text-sm text-gray-500">
                    Click a run to drill into tasks, criteria, artifacts,
                    and logs.
                </p>
            </div>

            <section className="border border-gray-200 rounded-lg bg-white p-4 space-y-4">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                    <div>
                        <h2 className="text-sm font-semibold text-gray-900">
                            Daily Success Rate (%)
                        </h2>
                        <p className="text-xs text-gray-500">
                            {activeTag || q ? (
                                <>
                                    Scoped to{" "}
                                    {activeTag && (
                                        <>
                                            tag{" "}
                                            <span className="font-mono text-gray-700">
                                                {activeTag}
                                            </span>
                                        </>
                                    )}
                                    {activeTag && q && " and "}
                                    {q && (
                                        <>
                                            search{" "}
                                            <span className="font-mono text-gray-700">
                                                {q}
                                            </span>
                                        </>
                                    )}{" "}
                                    over the last {window} ·{" "}
                                    {overview.runs.length} run
                                    {overview.runs.length === 1 ? "" : "s"}
                                    {" · "}
                                    <Link
                                        href={buildHref({ window })}
                                        scroll={false}
                                        className="text-studio-blue hover:underline"
                                    >
                                        clear
                                    </Link>
                                </>
                            ) : (
                                <>
                                    Success rate per run across the last{" "}
                                    {window} · {overview.runs.length} run
                                    {overview.runs.length === 1 ? "" : "s"}
                                </>
                            )}
                        </p>
                    </div>
                    <WindowSelector current={window} />
                </div>
                <DailySuccessChart
                    data={overview.runs}
                    windowStart={overview.windowStart}
                    windowEnd={overview.windowEnd}
                />
                <div className="pt-4 mt-2 border-t border-dashed border-gray-200 space-y-1">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Within Expected Turns (%)
                    </h2>
                    <p className="text-xs text-gray-500">
                        % of budgeted tasks that stayed within 1.5× their
                        expected turns (a budgeted task that failed counts as
                        over budget)
                        {activeTag || q ? " · scoped to the active filter" : ""}
                    </p>
                    <TurnBudgetChart
                        data={overview.runs}
                        windowStart={overview.windowStart}
                        windowEnd={overview.windowEnd}
                    />
                </div>
                <div className="pt-2 border-t border-gray-100 space-y-2">
                    <ChipLegend />
                    <CollapsibleRail id="home-tagrail">
                        <MergedTagRail
                            skills={skills}
                            taskTags={taskTags}
                            reviewTags={reviewTags}
                            activeTag={activeTag}
                            window={window}
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
                </div>
            </section>

            <div className="flex items-baseline justify-between gap-3 pt-1">
                <div className="flex items-baseline gap-3 flex-wrap">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Runs
                    </h2>
                    <span className="text-xs text-gray-500 tabular-nums">
                        {isFiltered
                            ? `${shownCount} of ${matchedCount} matching · ${totalInWindow} in ${window}`
                            : `${shownCount} of ${totalInWindow} in ${window}`}
                    </span>
                    {isFiltered && (
                        <Link
                            href={clearAllHref}
                            scroll={false}
                            className="text-xs text-gray-500 hover:text-gray-900 underline"
                        >
                            clear filter
                        </Link>
                    )}
                </div>
            </div>

            <TableScroll
                footer={
                    hasMore ? (
                        <div className="flex items-center justify-center gap-3 px-4 py-3 border-t border-gray-100 bg-gray-50 text-xs">
                            <Link
                                href={showMoreHref}
                                scroll={false}
                                className="text-studio-blue hover:underline"
                            >
                                Show{" "}
                                {Math.min(
                                    DEFAULT_LIMIT,
                                    tableTotalLabel - shownCount,
                                )}{" "}
                                more
                            </Link>
                            <span className="text-gray-300">·</span>
                            <Link
                                href={showAllHref}
                                scroll={false}
                                className="text-studio-blue hover:underline"
                            >
                                Show all ({tableTotalLabel})
                            </Link>
                        </div>
                    ) : undefined
                }
            >
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className="py-3 px-4 font-medium">Run</th>
                            <th className="py-3 px-4 font-medium">
                                Pass rate
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                {isFiltered ? "Matching tasks" : "Tasks"}
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                Cost
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                Duration
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {listing.rows.map((r) => {
                            const total = r.tasksRun;
                            const pct = total
                                ? (r.tasksSucceeded / total) * 100
                                : null;
                            return (
                                <tr
                                    key={r.id}
                                    className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                                >
                                    <td className="py-3 px-4">
                                        <Link
                                            href={`/runs/${r.id}`}
                                            className="font-mono text-xs text-gray-900 hover:text-studio-blue font-semibold tabular-nums"
                                        >
                                            {fmtRunTime(r.id)}
                                        </Link>
                                    </td>
                                    <td className="py-3 px-4 tabular-nums">
                                        <span
                                            className={`font-medium ${passClass(
                                                pct,
                                                total > 0,
                                            )}`}
                                        >
                                            {pct != null
                                                ? `${pct.toFixed(0)}%`
                                                : "—"}
                                        </span>
                                        <span className="text-xs text-gray-500 ml-2 tabular-nums">
                                            {r.tasksSucceeded}/{total}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                        {total}
                                    </td>
                                    <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                        {fmtCost(r.totalCostUsd)}
                                    </td>
                                    <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                        {fmtDuration(r.taskDurationSeconds)}
                                    </td>
                                </tr>
                            );
                        })}
                        {listing.rows.length === 0 && (
                            <tr>
                                <td
                                    colSpan={5}
                                    className="py-6 px-4 text-center text-sm text-gray-500"
                                >
                                    {isFiltered
                                        ? "no runs match the current filter"
                                        : "no runs yet"}
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </TableScroll>

            {adhocRows.length > 0 && (
                <div className="space-y-2 pt-2">
                    <div className="flex items-baseline gap-3 flex-wrap">
                        <h2 className="text-sm font-semibold text-gray-900">
                            Ad-hoc runs
                        </h2>
                        <span className="text-xs text-gray-500">
                            Manually uploaded · excluded from the chart, run
                            list, and trends above
                        </span>
                    </div>
                    <TableScroll>
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                    <th className="py-3 px-4 font-medium">
                                        Run
                                    </th>
                                    <th className="py-3 px-4 font-medium">
                                        Pass rate
                                    </th>
                                    <th className="py-3 px-4 font-medium text-right">
                                        Tasks
                                    </th>
                                    <th className="py-3 px-4 font-medium text-right">
                                        Cost
                                    </th>
                                    <th className="py-3 px-4 font-medium text-right">
                                        Duration
                                    </th>
                                </tr>
                            </thead>
                            <tbody>
                                {adhocRows.map((r) => {
                                    const total = r.tasksRun;
                                    const pct = total
                                        ? (r.tasksSucceeded / total) * 100
                                        : null;
                                    return (
                                        <tr
                                            key={r.id}
                                            className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                                        >
                                            <td className="py-3 px-4">
                                                <Link
                                                    href={`/runs/${r.id}`}
                                                    className="text-studio-blue hover:underline"
                                                >
                                                    {r.title ? (
                                                        <span className="font-medium text-gray-900">
                                                            {r.title}
                                                        </span>
                                                    ) : (
                                                        <span className="font-mono text-xs">
                                                            {r.id}
                                                        </span>
                                                    )}
                                                </Link>
                                                {r.title && (
                                                    <div className="font-mono text-[11px] text-gray-400">
                                                        {r.id}
                                                    </div>
                                                )}
                                            </td>
                                            <td className="py-3 px-4 tabular-nums">
                                                <span
                                                    className={`font-medium ${passClass(
                                                        pct,
                                                        total > 0,
                                                    )}`}
                                                >
                                                    {pct != null
                                                        ? `${pct.toFixed(0)}%`
                                                        : "—"}
                                                </span>
                                                <span className="text-xs text-gray-500 ml-2 tabular-nums">
                                                    {r.tasksSucceeded}/{total}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                                {total}
                                            </td>
                                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                                {fmtCost(r.totalCostUsd)}
                                            </td>
                                            <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                                {fmtDuration(
                                                    r.taskDurationSeconds,
                                                )}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </TableScroll>
                </div>
            )}
        </div>
    );
}
