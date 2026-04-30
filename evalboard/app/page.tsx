import Link from "next/link";
import { recentRunSummaries } from "@/lib/runs";
import { fmtDuration, fmtRunTime } from "@/lib/format";
import { ADX_URL } from "@/lib/config";

export const dynamic = "force-dynamic";

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

export default async function Page() {
    const runs = await recentRunSummaries(20);

    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">
                    Recent runs
                </h1>
                <p className="text-sm text-gray-500">
                    Click a run to drill into tasks, criteria, artifacts,
                    and logs. For trends, heatmaps, and time-range filtering,{" "}
                    <a
                        href={ADX_URL}
                        target="_blank"
                        rel="noreferrer"
                        className="text-studio-blue hover:underline inline-flex items-center gap-0.5"
                    >
                        see the ADX dashboard
                        <span className="text-xs">↗</span>
                    </a>
                    .
                </p>
            </div>

            <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className="py-3 px-4 font-medium">Run</th>
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
                        {runs.map((r) => {
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
                        {runs.length === 0 && (
                            <tr>
                                <td
                                    colSpan={5}
                                    className="py-6 px-4 text-center text-sm text-gray-500"
                                >
                                    no runs yet
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
