import Link from "next/link";
import type { TagTaskRow } from "@/lib/overview";
import { fmtRunDate, humanizeTaskId } from "@/lib/format";
import { passClass } from "@/lib/pass-rate";
import { matureAggregateTooltip, MaturePill, StatusPill } from "@/lib/pills";
import { TableScroll } from "../_components/scroll-table";
import { type Window } from "@/lib/reviews-types";

// The Path-to-GA task table. Split out of page.tsx (which stays the async IO
// shell) purely so it is render-testable in jsdom — mirrors the
// app/runs/[id]/page.tsx + run-view.tsx split. No "use client": this holds no
// state and no handlers, and a server component may render the "use client"
// TableScroll as a child.
//
// Every row here is scored on runs that ACTUALLY EXECUTED (see
// lib/overview.ts::buildTagTaskRows), which is narrower than the headline tile
// and chart above it — hence the caveat paragraph in page.tsx. A mature
// carry-forward's inherited status/score are dashed out rather than shown as if
// they were measured (same idiom as app/trends/trends-view.tsx; note
// app/runs/[id]/task-grid.tsx deliberately still shows the carried-forward 1.00
// beside its own MaturePill, so the two surfaces differ). Not extracted into a
// shared helper — the column shapes differ, so it would be a wrapper around a
// ternary.
function passRateTooltip(r: TagTaskRow): string {
    // Read off the row, never re-derived: if the exclusion set ever widens the
    // caption must move with the percentage it describes.
    const executed = r.executed;
    if (executed === 0) {
        return (
            `Not executed once in this window — all ${r.appearances} appearance` +
            `${r.appearances === 1 ? "" : "s"} were mature carry-forwards, so ` +
            "there is no measured pass rate."
        );
    }
    return (
        `Measured over ${executed} executed appearance` +
        `${executed === 1 ? "" : "s"}` +
        (r.matureSkips > 0
            ? ` (${r.matureSkips} mature carry-forward${r.matureSkips === 1 ? "" : "s"} excluded).`
            : ".")
    );
}

// `windowLabel`, not `window`: a prop named `window` shadows the DOM global
// inside the component body, and sibling components in this tree genuinely use
// that global (app/_components/scroll-table.tsx, app/_components/search-box.tsx).
export function TagTaskTable({
    rows,
    tag,
    windowLabel,
    harness,
}: {
    rows: TagTaskRow[];
    tag: string;
    windowLabel: Window;
    harness: string | null;
}) {
    return (
        <div className="space-y-2">
            <div className="flex flex-wrap items-baseline gap-2">
                <h2 className="text-sm font-semibold text-gray-900">Tasks</h2>
                {/* Unscoped, a task's appearances span harnesses, so its rate
                    pools regimes that aren't strictly comparable — and the row
                    then mixes scopes: Appearances/Pass rate are pooled, while
                    Last appearance and the two Latest columns describe ONE run (and
                    maturity is per-harness pipeline state, so a Mature pill can
                    legitimately sit beside a middling pooled rate). Say both,
                    rather than let either read as one harness's. */}
                {!harness && (
                    <span className="text-xs text-gray-500">
                        pooled across harnesses · Last appearance and Latest
                        describe a
                        single run · pick one above to separate them
                    </span>
                )}
            </div>
            <TableScroll>
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className="py-3 px-4 font-medium">Task</th>
                            <th className="py-3 px-4 font-medium">Skill</th>
                            <th className="py-3 px-4 font-medium text-right">
                                Appearances
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                Last appearance
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                Pass rate
                            </th>
                            <th className="py-3 px-4 font-medium">
                                Latest status
                            </th>
                            <th className="py-3 px-4 font-medium text-right">
                                Latest score
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((r) => (
                            <tr
                                key={r.taskId}
                                className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors"
                            >
                                <td className="py-3 px-4">
                                    <Link
                                        href={`/runs/${r.latestRunId}`}
                                        className="text-gray-900 hover:text-studio-blue font-medium"
                                    >
                                        {humanizeTaskId(r.taskId)}
                                    </Link>
                                    <div className="font-mono text-[11px] text-gray-400">
                                        {r.taskId}
                                    </div>
                                </td>
                                <td className="py-3 px-4 text-gray-700">
                                    {r.skill ?? "—"}
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                    {r.appearances}
                                    {r.matureSkips > 0 && (
                                        <span
                                            className="text-gray-500"
                                            title={matureAggregateTooltip(
                                                r.matureSkips,
                                                r.appearances,
                                            )}
                                        >
                                            {" "}
                                            ({r.matureSkips} mature)
                                        </span>
                                    )}
                                </td>
                                {/* No conditional dimming: under harness rotation
                                    "newest" differs per harness, so a relative
                                    highlight would mislead. */}
                                <td
                                    className="py-3 px-4 text-right tabular-nums text-gray-500"
                                    title={`Newest run in the window that carried this task under ${tag}. It may be a mature carry-forward, so this is when the task last APPEARED, not necessarily when it last executed — see the Latest status cell.`}
                                >
                                    {fmtRunDate(r.latestRunId)}
                                </td>
                                {/* The denominator is `appearances - matureSkips`,
                                    which is NOT the Appearances shown two cells
                                    left — a mature task is re-validated about
                                    weekly, so a 24-appearance row can rest on a
                                    single executed run and still tint green.
                                    Name the sample size on hover so a confident
                                    percentage can't hide a tiny denominator. */}
                                <td
                                    className="py-3 px-4 text-right tabular-nums"
                                    title={passRateTooltip(r)}
                                >
                                    <span
                                        className={`font-medium ${passClass(r.passRate)}`}
                                    >
                                        {r.passRate != null
                                            ? `${r.passRate.toFixed(0)}%`
                                            : "—"}
                                    </span>
                                </td>
                                <td className="py-3 px-4">
                                    {r.latestMatureSkipped ? (
                                        <MaturePill />
                                    ) : (
                                        <StatusPill
                                            status={r.latestStatus}
                                            relabel
                                        />
                                    )}
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                    {r.latestMatureSkipped ||
                                    r.latestScore == null
                                        ? "—"
                                        : r.latestScore.toFixed(2)}
                                </td>
                            </tr>
                        ))}
                        {rows.length === 0 && (
                            <tr>
                                <td
                                    colSpan={7}
                                    className="py-6 px-4 text-center text-sm text-gray-500"
                                >
                                    No tasks tagged {tag} in the last{" "}
                                    {windowLabel}.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </TableScroll>
        </div>
    );
}
