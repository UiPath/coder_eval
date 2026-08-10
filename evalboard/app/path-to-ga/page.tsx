import {
    avgRunSuccessRate,
    getOverview,
    getTagTaskBreakdown,
    listRecentHarnesses,
} from "@/lib/overview";
import { parseHarnessScope } from "@/lib/harness";
import { HarnessSelector } from "../_components/harness-selector";
import { harnessShortLabel } from "../_components/harness-badge";
import { type Window } from "@/lib/reviews-types";
import { DailySuccessChart } from "../_overview/daily-chart";
import { TagTaskTable } from "./task-table";

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
    const avgPassRate = avgRunSuccessRate(overview.runs);

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
                            distinct task{taskRows.length === 1 ? "" : "s"} still
                            tagged
                        </div>
                    </div>
                </div>
                {/* The tile above and the chart below keep their original
                    mature-blind, union-over-the-window semantics (they feed the
                    front page and every tag-filtered view); the table does not.
                    Say so, rather than let the two silently disagree. */}
                <p className="text-xs text-gray-500">
                    The rate above and the chart cover every run that carried a{" "}
                    <span className="font-mono">{TAG}</span> task at the time it
                    ran, counting mature carry-forwards as passes. The table
                    below is narrower: only tasks still carrying the tag, scored
                    on runs that actually executed.
                </p>
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

            <TagTaskTable
                rows={taskRows}
                tag={TAG}
                windowLabel={WINDOW}
                harness={harness}
            />
        </div>
    );
}
