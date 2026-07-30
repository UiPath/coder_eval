import Link from "next/link";
import {
    getAdhocRunListing,
    getOverview,
    getRunListing,
    getWindowRollup,
    listRecentHarnesses,
    type TagCount,
} from "@/lib/overview";
import { parseHarnessScope } from "@/lib/harness";
import { fmtDuration, fmtRunTime, fmtTimestamp } from "@/lib/format";
import { passClass } from "@/lib/pass-rate";
import { type Window } from "@/lib/reviews-types";
import { DailySuccessChart } from "./_overview/daily-chart";
import { TurnBudgetChart } from "./_overview/turn-budget-chart";
import { WindowSummary } from "./_overview/window-summary";
import { ChipLegend, MergedTagRail } from "./_overview/tag-rail";
import { TableScroll } from "./_components/scroll-table";
import { CollapsibleRail } from "./_components/collapsible-rail";
import { isInternal } from "@/lib/edition";
import { HarnessBadge, harnessShortLabel } from "@/app/_components/harness-badge";
import { HarnessSelector } from "@/app/_components/harness-selector";

export const dynamic = "force-dynamic";

const DEFAULT_LIMIT = 20;
const ADHOC_LIMIT = 10;

// The charts and the summary tiles cover a fixed 30 days. There is no window
// control: the run table pages back through all of history on its own (see
// getRunListing), which is what a shorter window was really being used for, and
// a 30-day chart is the one that shows a trend rather than a few points.
const WINDOW: Window = "30d";

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

// Hard ceiling on how far the tables can be paged out. Both sections grow a
// page at a time and never expose a "show all" jump: every extra row is another
// multi-MB run.json read, so an unbounded jump is the one interaction that can
// stall the page. Well above the store's run count, so in practice you can keep
// expanding to the oldest run; a hand-typed `?limit=` above this clamps here.
const MAX_LIMIT = 500;

function parsePagedLimit(
    raw: string | string[] | undefined,
    fallback: number,
): number {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return fallback;
    const n = parseInt(v, 10);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    return Math.min(n, MAX_LIMIT);
}

function parseLimit(raw: string | string[] | undefined): number {
    return parsePagedLimit(raw, DEFAULT_LIMIT);
}

// Separate from the main table's `limit` so expanding one section doesn't
// reset the other's pagination.
function parseAdhocLimit(raw: string | string[] | undefined): number {
    return parsePagedLimit(raw, ADHOC_LIMIT);
}

function fmtCost(c: number | null): string {
    if (c == null) return "—";
    return `$${c.toFixed(2)}`;
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
        tag?: string;
        q?: string;
        h?: string;
        limit?: string;
        alimit?: string;
    }>;
}) {
    const params = await searchParams;
    const activeTag = parseTag(params.tag);
    const q = parseQ(params.q);
    // null = every harness, and that is the default: the page opens on the
    // cross-harness comparison and narrows from there.
    const harness = parseHarnessScope(params.h);
    const limit = parseLimit(params.limit);
    const adhocLimit = parseAdhocLimit(params.alimit);
    const isFiltered = activeTag != null || q != null;

    // One harness scope drives everything on the page: the charts split into a
    // line per harness in the window, and the summary tiles + run list are
    // filtered to the same set. Picking a harness therefore recomputes the
    // tiles and the charts and narrows the table, instead of re-scoping the
    // analytics block while the table silently kept showing every harness.
    const [overview, rollup, listing, adhoc, harnesses] = await Promise.all([
        getOverview(WINDOW, activeTag, q, harness),
        getWindowRollup(WINDOW, activeTag, q, harness),
        getRunListing(activeTag, q, limit, harness),
        getAdhocRunListing(adhocLimit),
        listRecentHarnesses(),
    ]);

    const skills = filterTagsByQuery(overview.skills, q);
    const taskTags = filterTagsByQuery(overview.taskTags, q);
    const reviewTags = filterTagsByQuery(overview.reviewTags, q);

    const shownCount = listing.rows.length;
    // Another page exists AND the page cap still has room for it. Under a
    // filter there is no total to count against: proving how many older runs
    // match would mean reading all of them, so the table reports what it has
    // and offers another page while one is there.
    const hasMore = listing.hasMore && shownCount < MAX_LIMIT;
    const tableCountLabel = isFiltered
        ? `${shownCount} matching`
        : `${shownCount} of ${listing.totalCandidates}`;

    // Current URL params, normalized to scalars. Spread as the base of every
    // href so toggling one section (main `limit` or ad-hoc `alimit`) carries
    // the other's state through instead of silently resetting it.
    const rawLimit = Array.isArray(params.limit) ? params.limit[0] : params.limit;
    const rawAlimit = Array.isArray(params.alimit)
        ? params.alimit[0]
        : params.alimit;
    // Omit the all-harness scope from URLs to keep them clean; carry a narrowed
    // scope through every self-link so it isn't reset by pagination/clear.
    const hParam = harness ?? undefined;
    const base = {
        tag: activeTag,
        q,
        h: hParam,
        limit: rawLimit,
        alimit: rawAlimit,
    };

    // Both tables grow one page at a time. There is deliberately no "show all"
    // link: each row is backed by a run.json read, so an unbounded jump is the
    // one click that can hang the page.
    const nextPageSize = Math.min(DEFAULT_LIMIT, MAX_LIMIT - shownCount);
    const showMoreHref = buildHref({
        ...base,
        limit: shownCount + nextPageSize,
    });
    const clearAllHref = buildHref({ h: hParam });

    // Ad-hoc section disclosure: rows are filtered (by `q`) then capped to
    // adhocLimit; page in another ADHOC_LIMIT while more match than are shown,
    // and offer a collapse back to the default once expanded past it.
    const adhocRemaining = Math.min(adhoc.total, MAX_LIMIT) - adhoc.rows.length;
    const adhocExpandable = adhocRemaining > 0;
    const adhocNextPageSize = Math.min(ADHOC_LIMIT, adhocRemaining);
    const adhocExpanded = adhoc.rows.length > ADHOC_LIMIT;
    const adhocShowMoreHref = buildHref({
        ...base,
        alimit: adhoc.rows.length + adhocNextPageSize,
    });
    const adhocShowLessHref = buildHref({ ...base, alimit: undefined });

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

            <WindowSummary
                totals={rollup.totals}
                window={WINDOW}
                runCount={rollup.runCount}
                isFiltered={isFiltered}
            />

            {/* The analytics block — daily success / turn-budget charts, the
                harness selector, and the colored skill/review/tag rail — is an
                internal-only surface (see lib/edition.ts). The public OSS
                edition drops it so the front page is just the run list. */}
            {isInternal && (
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
                                    over the last {WINDOW} ·{" "}
                                    {overview.runs.length} run
                                    {overview.runs.length === 1 ? "" : "s"}
                                    {" · "}
                                    <Link
                                        href={buildHref({ h: hParam })}
                                        scroll={false}
                                        className="text-studio-blue hover:underline"
                                    >
                                        clear
                                    </Link>
                                </>
                            ) : (
                                <>
                                    Success rate per{" "}
                                    {harness
                                        ? `${harnessShortLabel(harness)} run`
                                        : "run, one line per harness,"}{" "}
                                    across the last {WINDOW} ·{" "}
                                    {overview.runs.length} run
                                    {overview.runs.length === 1 ? "" : "s"}
                                </>
                            )}
                        </p>
                    </div>
                    {/* min-w-0 + flex-wrap: the two segmented controls are wider
                        than a phone together, so they stack rather than pushing
                        the whole page past the viewport. */}
                    <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
                        <HarnessSelector
                            current={harness}
                            harnesses={harnesses}
                            includeAll
                        />
                    </div>
                </div>
                <DailySuccessChart
                    data={overview.runs}
                    harnesses={overview.harnesses}
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
                        harnesses={overview.harnesses}
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
                            window={WINDOW}
                            q={q}
                            harness={harness}
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
            )}

            <div className="flex items-baseline justify-between gap-3 pt-1">
                <div className="flex items-baseline gap-3 flex-wrap">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Runs
                    </h2>
                    <span className="text-xs text-gray-500 tabular-nums">
                        {tableCountLabel}
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
                                Show {nextPageSize} more
                            </Link>
                            <span className="text-gray-400 tabular-nums">
                                {tableCountLabel}
                            </span>
                        </div>
                    ) : undefined
                }
            >
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className="py-3 px-4 font-medium">Run</th>
                            {isInternal && (
                                <th className="py-3 px-4 font-medium">
                                    Harness
                                </th>
                            )}
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
                                    {/* nowrap: the table already scrolls inside
                                        its own container, so a narrow screen
                                        should scroll it rather than break the
                                        timestamp across three lines. */}
                                    <td className="py-3 px-4 whitespace-nowrap">
                                        <Link
                                            href={`/runs/${r.id}`}
                                            className="font-mono text-xs text-gray-900 hover:text-studio-blue font-semibold tabular-nums"
                                        >
                                            {fmtRunTime(r.id)}
                                        </Link>
                                    </td>
                                    {isInternal && (
                                        <td className="py-3 px-4">
                                            <HarnessBadge
                                                harness={r.harness}
                                            />
                                        </td>
                                    )}
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
                                    colSpan={isInternal ? 6 : 5}
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

            {adhoc.total > 0 && (
                <div className="space-y-2 pt-2">
                    <div className="flex items-baseline gap-3 flex-wrap">
                        <h2 className="text-sm font-semibold text-gray-900">
                            Ad-hoc runs
                        </h2>
                        <span className="text-xs text-gray-500 tabular-nums">
                            {adhocExpandable
                                ? `${adhoc.rows.length} of ${adhoc.total}`
                                : `${adhoc.total}`}
                        </span>
                        <span className="text-xs text-gray-500">
                            Manually uploaded · excluded from the chart, run
                            list, and trends above
                        </span>
                    </div>
                    <TableScroll
                        footer={
                            adhocExpandable || adhocExpanded ? (
                                <div className="flex items-center justify-center gap-3 px-4 py-3 border-t border-gray-100 bg-gray-50 text-xs">
                                    {adhocExpandable && (
                                        <Link
                                            href={adhocShowMoreHref}
                                            scroll={false}
                                            className="text-studio-blue hover:underline"
                                        >
                                            Show {adhocNextPageSize} more
                                        </Link>
                                    )}
                                    <span className="text-gray-400 tabular-nums">
                                        {adhoc.rows.length} of {adhoc.total}
                                    </span>
                                    {adhocExpanded && (
                                        <Link
                                            href={adhocShowLessHref}
                                            scroll={false}
                                            className="text-studio-blue hover:underline"
                                        >
                                            Show fewer
                                        </Link>
                                    )}
                                </div>
                            ) : undefined
                        }
                    >
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                    <th className="py-3 px-4 font-medium">
                                        Run
                                    </th>
                                    <th className="py-3 px-4 font-medium">
                                        Date
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
                                {adhoc.rows.map((r) => {
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
                                                    className="text-gray-900 hover:text-studio-blue"
                                                >
                                                    {r.title ? (
                                                        <span className="font-medium">
                                                            {r.title}
                                                        </span>
                                                    ) : (
                                                        <span className="font-mono text-xs font-semibold tabular-nums">
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
                                            <td className="py-3 px-4 font-mono text-xs text-gray-700 tabular-nums whitespace-nowrap">
                                                {fmtTimestamp(r.startedAt)}
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
