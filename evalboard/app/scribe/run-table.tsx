import Link from "next/link";
import { fmtDuration, fmtRunTime, fmtUsd } from "@/lib/format";
import { passClass } from "@/lib/pass-rate";
import { type RunListingRow } from "@/lib/overview";
import { SCRIBE_SOURCE } from "@/lib/sources";
import { TableScroll } from "../_components/scroll-table";

function passPct(row: RunListingRow): number | null {
    if (row.tasksRun === 0) return null;
    return (row.tasksSucceeded / row.tasksRun) * 100;
}

export function ScribeRunTable({
    rows,
    totalCandidates,
    showMoreHref,
}: {
    rows: RunListingRow[];
    totalCandidates: number;
    showMoreHref: string | null;
}) {
    if (rows.length === 0) {
        return (
            <section className="border border-gray-200 rounded-lg bg-white p-4">
                <p className="text-sm text-gray-500 py-6 text-center">
                    No runs to show.
                </p>
            </section>
        );
    }

    return (
        <section className="space-y-2">
            <div className="flex items-baseline justify-between gap-3">
                <h2 className="text-sm font-semibold text-gray-900">Runs</h2>
                <span className="text-xs text-gray-500 tabular-nums">
                    {rows.length} of {totalCandidates}
                </span>
            </div>
            <TableScroll
                footer={
                    showMoreHref ? (
                        <a
                            href={showMoreHref}
                            className="block px-3 py-2 text-center text-xs text-studio-blue hover:underline"
                        >
                            Show more
                        </a>
                    ) : undefined
                }
            >
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                            <th className="px-3 py-2 font-medium">Run</th>
                            <th className="px-3 py-2 font-medium text-right">
                                Pass
                            </th>
                            <th className="px-3 py-2 font-medium text-right">
                                Tasks
                            </th>
                            <th className="px-3 py-2 font-medium text-right">
                                Duration
                            </th>
                            <th className="px-3 py-2 font-medium text-right">
                                Cost
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => {
                            const pct = passPct(row);
                            return (
                                <tr
                                    key={row.id}
                                    className="border-b border-gray-100 last:border-0 hover:bg-gray-50"
                                >
                                    <td className="px-3 py-2">
                                        {/* `src` is required, not decorative: run ids
                                            are only unique within a container, so a
                                            source-blind /runs/<id> would resolve this
                                            id against the skills nightly's container
                                            and could render a different run entirely. */}
                                        <Link
                                            href={`/runs/${row.id}?src=${SCRIBE_SOURCE.id}`}
                                            className="font-mono text-studio-blue hover:underline"
                                        >
                                            {fmtRunTime(row.id)}
                                        </Link>
                                    </td>
                                    <td
                                        className={`px-3 py-2 text-right tabular-nums ${passClass(pct)}`}
                                    >
                                        {pct != null
                                            ? `${pct.toFixed(0)}%`
                                            : "—"}
                                    </td>
                                    <td className="px-3 py-2 text-right tabular-nums text-gray-700">
                                        {row.tasksSucceeded}/{row.tasksRun}
                                    </td>
                                    <td className="px-3 py-2 text-right tabular-nums text-gray-700">
                                        {fmtDuration(row.taskDurationSeconds)}
                                    </td>
                                    <td className="px-3 py-2 text-right tabular-nums text-gray-700">
                                        {fmtUsd(row.totalCostUsd)}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </TableScroll>
        </section>
    );
}
