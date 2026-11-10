import Link from "next/link";
import { Suspense } from "react";
import { fmtUtcMinute } from "@/lib/format";
import type { SeriesArm, SeriesDef } from "@/lib/series";
import type { ArmStat, SeriesDetailRun } from "@/lib/series-data";
import { withSource } from "@/app/_lib/source-param";
import { TableScroll } from "@/app/_components/scroll-table";
import { ReasonText } from "../_components/reason-text";
import { RunCost } from "./run-cost";

function ArmCells({
    arms,
    stats,
}: {
    arms: readonly SeriesArm[];
    stats: Record<string, ArmStat> | undefined;
}) {
    if (!stats) return <span className="text-gray-400">no data</span>;
    return (
        <span className="whitespace-nowrap">
            {arms.map((a) => {
                const s = stats[a.id];
                return (
                    <span key={a.id} className="mr-3">
                        {arms.length > 1 && (
                            <span className="text-gray-500">{a.label} </span>
                        )}
                        {s?.passRate != null ? `${Math.round(s.passRate)}%` : "—"}
                        {s && (
                            <span className="ml-1 text-xs text-gray-500">
                                {s.passed}/{s.graded}
                            </span>
                        )}
                    </span>
                );
            })}
        </span>
    );
}

export function RunList({
    def,
    arms,
    runs,
    total,
    moreHref,
}: {
    def: SeriesDef;
    arms: readonly SeriesArm[];
    runs: readonly SeriesDetailRun[];
    total: number;
    moreHref: string | null;
}) {
    const sourceId = def.source.id;
    return (
        <section className="space-y-2">
            <div className="flex items-baseline gap-3 flex-wrap">
                <h2 className="text-sm font-semibold text-gray-900">Runs</h2>
                <span className="text-xs text-gray-500 tabular-nums">
                    {runs.length} of {total}
                </span>
                <span className="text-xs text-gray-500">
                    Newest first · excluded runs are greyed out and left off the
                    trend
                </span>
            </div>
            <TableScroll
                footer={
                    moreHref ? (
                        <Link
                            href={moreHref}
                            scroll={false}
                            className="block px-3 py-2 text-center text-xs text-studio-blue hover:underline"
                        >
                            Show more
                        </Link>
                    ) : undefined
                }
            >
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className="py-3 px-4 font-medium">Run</th>
                            <th className="py-3 px-4 font-medium">Started (UTC)</th>
                            <th className="py-3 px-4 font-medium">Pass rate</th>
                            <th className="py-3 px-4 font-medium text-right">
                                Cost
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs.map(({ ref, stats }) => (
                            <tr
                                key={ref.id}
                                data-excluded={ref.excluded ? "true" : undefined}
                                className={`border-b border-gray-100 last:border-b-0 ${
                                    ref.excluded
                                        ? "bg-gray-50 text-gray-400"
                                        : "hover:bg-gray-50 transition-colors"
                                }`}
                            >
                                <td className="py-3 px-4">
                                    <Link
                                        href={withSource(`/runs/${ref.id}`, sourceId)}
                                        className={
                                            ref.excluded
                                                ? "hover:text-studio-blue"
                                                : "text-gray-900 hover:text-studio-blue"
                                        }
                                    >
                                        <span className="font-medium">
                                            {ref.title ?? ref.id}
                                        </span>
                                    </Link>
                                    {ref.title && (
                                        <div className="font-mono text-[11px] text-gray-400">
                                            {ref.id}
                                        </div>
                                    )}
                                    {ref.excluded && (
                                        <div className="mt-1 text-xs text-gray-500">
                                            <span className="mr-1.5 rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[10px] uppercase tracking-wide font-semibold text-gray-500">
                                                Excluded
                                            </span>
                                            <ReasonText
                                                reason={ref.excluded}
                                                sourceId={sourceId}
                                            />
                                        </div>
                                    )}
                                </td>
                                <td className="py-3 px-4 font-mono text-xs tabular-nums whitespace-nowrap">
                                    {fmtUtcMinute(ref.startMs)}
                                </td>
                                <td className="py-3 px-4 tabular-nums">
                                    <ArmCells arms={arms} stats={stats?.arms} />
                                </td>
                                <td className="py-3 px-4 text-right tabular-nums">
                                    <Suspense fallback="…">
                                        <RunCost def={def} id={ref.id} />
                                    </Suspense>
                                </td>
                            </tr>
                        ))}
                        {runs.length === 0 && (
                            <tr>
                                <td
                                    colSpan={4}
                                    className="py-8 px-4 text-center text-gray-500"
                                >
                                    No runs found for this series.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </TableScroll>
        </section>
    );
}
