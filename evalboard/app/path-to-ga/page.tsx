import Link from "next/link";
import {
    getOverview,
    getTagTaskBreakdown,
    listRecentHarnesses,
} from "@/lib/overview";
import { parseHarnessScope } from "@/lib/harness";
import { humanizeTaskId } from "@/lib/format";
import { passClass } from "@/lib/pass-rate";
import { HarnessSelector } from "../_components/harness-selector";
import { harnessShortLabel } from "../_components/harness-badge";
import { type Window } from "@/lib/reviews-types";
import { DailySuccessChart } from "../_overview/daily-chart";
import { TableScroll } from "../_components/scroll-table";
import { StatusPill } from "@/lib/pills";

export const dynamic = "force-dynamic";

const TAG = "path-to-ga";

// Fixed 30-day window, matching the front page: no window control, since the
// readiness question is "where is this task now, and how did it get here" rather
// than "how did it look in a 1-day slice".
const WINDOW: Window = "30d";

export default async function PathToGaPage({
    searchParams,
}: {
    searchParams: Promise<{ h?: string }>;
}) {
    const params = await searchParams;
    // null = every harness, and that is the default. The chart draws a line per
    // harness, so the readiness of a task on each is comparable side by side
    // rather than requiring four page loads. Picking a harness narrows the
    // chart, the headline numbers, and the task table together.
    const harness = parseHarnessScope(params.h);

    const [overview, taskRows, harnesses] = await Promise.all([
        getOverview(WINDOW, TAG, null, harness),
        getTagTaskBreakdown(WINDOW, TAG, harness),
        listRecentHarnesses(),
    ]);

    const runsInWindow = overview.runs.length;
    const avgPassRate =
        runsInWindow > 0
            ? overview.runs.reduce((sum, r) => sum + (r.successRate ?? 0), 0) /
              runsInWindow
            : null;

    return (
        <div className="space-y-6">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div className="space-y-1">
                    <h1 className="text-xl font-semibold text-gray-900">
                        Path to GA
                    </h1>
                    <p className="text-sm text-gray-500">
                        Score for every task tagged{" "}
                        <span className="font-mono text-gray-700">{TAG}</span>
                        {harness
                            ? ` on ${harnessShortLabel(harness)}.`
                            : ", across every harness."}
                    </p>
                </div>
                <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
                    <HarnessSelector
                        current={harness}
                        harnesses={harnesses}
                        includeAll
                    />
                </div>
            </div>

            <section className="border border-gray-200 rounded-lg bg-white p-4 space-y-4">
                <div className="flex flex-wrap items-baseline gap-8">
                    <div>
                        <div className="text-3xl font-semibold tabular-nums text-gray-900">
                            {avgPassRate != null
                                ? `${avgPassRate.toFixed(0)}%`
                                : "—"}
                        </div>
                        <div className="text-xs text-gray-500">
                            avg pass rate over the last {WINDOW}
                        </div>
                    </div>
                    <div>
                        <div className="text-3xl font-semibold tabular-nums text-gray-900">
                            {runsInWindow}
                        </div>
                        <div className="text-xs text-gray-500">
                            run{runsInWindow === 1 ? "" : "s"} with a {TAG}{" "}
                            task
                        </div>
                    </div>
                    <div>
                        <div className="text-3xl font-semibold tabular-nums text-gray-900">
                            {taskRows.length}
                        </div>
                        <div className="text-xs text-gray-500">
                            distinct task{taskRows.length === 1 ? "" : "s"}
                        </div>
                    </div>
                </div>
                {runsInWindow > 0 ? (
                    <DailySuccessChart
                        data={overview.runs}
                        harnesses={overview.harnesses}
                        windowStart={overview.windowStart}
                        windowEnd={overview.windowEnd}
                    />
                ) : (
                    <p className="text-sm text-gray-500 py-8 text-center">
                        No {TAG} runs in the last {WINDOW}.
                    </p>
                )}
            </section>

            <div className="space-y-2">
                <div className="flex flex-wrap items-baseline gap-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Tasks
                    </h2>
                    {/* Unscoped, a task's appearances span harnesses, so its rate
                        pools regimes that aren't strictly comparable. Say so
                        rather than let the number read as one harness's. */}
                    {!harness && (
                        <span className="text-xs text-gray-500">
                            pooled across harnesses · pick one above to separate
                            them
                        </span>
                    )}
                </div>
                <TableScroll>
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                <th className="py-3 px-4 font-medium">
                                    Task
                                </th>
                                <th className="py-3 px-4 font-medium">
                                    Skill
                                </th>
                                <th className="py-3 px-4 font-medium text-right">
                                    Appearances
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
                            {taskRows.map((r) => (
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
                                    </td>
                                    <td className="py-3 px-4 text-right tabular-nums">
                                        <span
                                            className={`font-medium ${passClass(r.passRate)}`}
                                        >
                                            {r.passRate != null
                                                ? `${r.passRate.toFixed(0)}%`
                                                : "—"}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
                                        <StatusPill
                                            status={r.latestStatus}
                                            relabel
                                        />
                                    </td>
                                    <td className="py-3 px-4 text-right tabular-nums text-gray-700">
                                        {r.latestScore != null
                                            ? r.latestScore.toFixed(2)
                                            : "—"}
                                    </td>
                                </tr>
                            ))}
                            {taskRows.length === 0 && (
                                <tr>
                                    <td
                                        colSpan={6}
                                        className="py-6 px-4 text-center text-sm text-gray-500"
                                    >
                                        No tasks tagged {TAG} in the last{" "}
                                        {WINDOW}.
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </TableScroll>
            </div>
        </div>
    );
}
