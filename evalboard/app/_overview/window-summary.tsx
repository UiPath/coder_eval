import { fmtDuration, fmtUsd } from "@/lib/format";
import { passClass } from "@/lib/pass-rate";
import type { RunListingTotals } from "@/lib/overview";
import type { Window } from "@/lib/reviews-types";

function Tile({
    label,
    value,
    sub,
    valueClass = "text-gray-900",
}: {
    label: string;
    value: string;
    sub?: string;
    valueClass?: string;
}) {
    return (
        <div className="px-4 py-3">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                {label}
            </div>
            <div
                className={`text-2xl font-semibold mt-1 tabular-nums ${valueClass}`}
            >
                {value}
            </div>
            {sub && (
                <div className="text-xs text-gray-500 tabular-nums mt-0.5">
                    {sub}
                </div>
            )}
        </div>
    );
}

// Front-page window rollup: total spend + shape of the runs in scope. Every
// tile is summed over the same set — `runCount` and `totals` both come from
// getWindowRollup, so the Runs tile can never disagree with the
// Cost/Tasks/Pass/Compute tiles. The totals are scoped to matching tasks
// whenever a filter is active.
//
// This describes the window the charts plot, NOT however far the run table below
// is paged out; the tiles carry the window in their sub-labels so the difference
// is legible.
export function WindowSummary({
    totals,
    window,
    runCount,
    isFiltered,
}: {
    totals: RunListingTotals;
    window: Window;
    runCount: number;
    isFiltered: boolean;
}) {
    const pct =
        totals.tasksRun > 0
            ? (totals.tasksSucceeded / totals.tasksRun) * 100
            : null;
    const scope = isFiltered ? "matching runs" : `runs · last ${window}`;

    return (
        <section className="border border-gray-200 rounded-lg bg-white">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 divide-y sm:divide-y-0 sm:divide-x divide-gray-100">
                <Tile
                    label="Total cost"
                    value={fmtUsd(totals.costUsd)}
                    sub={
                        totals.costPartial
                            ? "some runs missing cost"
                            : isFiltered
                              ? "matching tasks"
                              : `across ${window}`
                    }
                />
                <Tile
                    label="Runs"
                    value={runCount.toLocaleString()}
                    sub={scope}
                />
                <Tile
                    label="Tasks"
                    value={totals.tasksRun.toLocaleString()}
                    sub={`${totals.tasksSucceeded.toLocaleString()} passed`}
                />
                <Tile
                    label="Pass rate"
                    value={pct != null ? `${pct.toFixed(0)}%` : "—"}
                    valueClass={passClass(pct, totals.tasksRun > 0)}
                />
                <Tile
                    label="Compute time"
                    value={fmtDuration(totals.durationSeconds)}
                    sub={totals.durationPartial ? "some runs missing" : undefined}
                />
            </div>
        </section>
    );
}
