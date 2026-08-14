import {
    avgRunSuccessRate,
    getAdhocRunListing,
    getOverview,
    getRunListing,
} from "@/lib/overview";
import { SCRIBE_SOURCE } from "@/lib/sources";
import { type Window } from "@/lib/reviews-types";
import { DailySuccessChart } from "../_overview/daily-chart";
import { ScribeRunTable } from "./run-table";

export const dynamic = "force-dynamic";

// Fixed 30-day window, matching the front page and Path to GA. This suite runs
// on a manual trigger rather than nightly, so a shorter window can easily be
// empty; 30 days is the smallest one that reliably shows a trend.
const WINDOW: Window = "30d";

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 500;

// Ad-hoc runs are manual uploads (the pipeline's `adhoc` parameter), so the set
// is small by construction — no paging, just a cap.
const ADHOC_LIMIT = 20;

function parseLimit(raw: string | string[] | undefined): number {
    const v = Array.isArray(raw) ? raw[0] : raw;
    const n = v ? Number.parseInt(v, 10) : NaN;
    if (!Number.isFinite(n) || n < 1) return DEFAULT_LIMIT;
    return Math.min(n, MAX_LIMIT);
}

export default async function ScribePage({
    searchParams,
}: {
    searchParams: Promise<{ limit?: string | string[] }>;
}) {
    const params = await searchParams;
    const limit = parseLimit(params.limit);

    // Everything on this page reads SCRIBE_SOURCE's container (aria-runs), not
    // the skills nightly's. No harness selector: the aria suite runs on a single
    // harness (delegate-sdk), so a scope control would only ever have one entry.
    const [overview, listing, adhoc] = await Promise.all([
        getOverview(WINDOW, null, null, null, SCRIBE_SOURCE),
        getRunListing(null, null, limit, null, SCRIBE_SOURCE),
        // The pipeline's `adhoc` parameter uploads under an 'adhoc-<id>' prefix.
        // Those ids aren't date-shaped, so getRunListing filters them out — without
        // this section they'd be uploaded and then unreachable, since /scribe is the
        // only surface that reads this container.
        getAdhocRunListing(ADHOC_LIMIT, SCRIBE_SOURCE),
    ]);

    const runsInWindow = overview.runs.length;
    const avgPassRate = avgRunSuccessRate(overview.runs);
    const shownCount = listing.rows.length;
    const hasMore = listing.hasMore && shownCount < MAX_LIMIT;
    const nextPageSize = Math.min(DEFAULT_LIMIT, MAX_LIMIT - shownCount);
    // What the container actually holds, ad-hoc included. listing.totalCandidates
    // counts only date-shaped ids (getRunListing filters before reporting it), so
    // using it alone for a "runs in the container" tile understates the container
    // and, at 0, would assert emptiness while ad-hoc runs sat in it.
    const totalInContainer = listing.totalCandidates + adhoc.total;

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">Scribe</h1>
                <p className="text-sm text-gray-500">
                    Results from the Autopilot (aria/Composer) eval suite, run by
                    the{" "}
                    <span className="font-mono text-gray-700">
                        UiPath.Autopilot.Eval.Manual
                    </span>{" "}
                    pipeline and uploaded to the{" "}
                    <span className="font-mono text-gray-700">
                        {SCRIBE_SOURCE.container}
                    </span>{" "}
                    container.
                </p>
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
                            run{runsInWindow === 1 ? "" : "s"} in the last{" "}
                            {WINDOW}
                        </div>
                    </div>
                    <div>
                        <div className="text-3xl font-semibold tabular-nums text-gray-900">
                            {totalInContainer}
                        </div>
                        <div className="text-xs text-gray-500">
                            pipeline run{totalInContainer === 1 ? "" : "s"}{" "}
                            uploaded
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
                    // "Nothing readable here" and "nothing recent" look identical
                    // in the chart, so distinguish them. Deliberately phrased as
                    // "no runs found" rather than asserting the container is
                    // empty: this counts what evalboard could read and recognize,
                    // and a run whose id isn't date-shaped (and isn't ad-hoc
                    // either) would be invisible to both listings — so claiming
                    // emptiness would be a stronger statement than the data
                    // supports.
                    <p className="text-sm text-gray-500 py-8 text-center">
                        {totalInContainer === 0
                            ? `No runs found in the ${SCRIBE_SOURCE.container} container yet.`
                            : `No runs in the last ${WINDOW}.`}
                    </p>
                )}
            </section>

            <ScribeRunTable
                rows={listing.rows}
                totalCandidates={listing.totalCandidates}
                showMoreHref={
                    hasMore
                        ? `/scribe?limit=${shownCount + nextPageSize}`
                        : null
                }
            />

            {adhoc.total > 0 && (
                <ScribeRunTable
                    rows={adhoc.rows}
                    totalCandidates={adhoc.total}
                    showMoreHref={null}
                    heading="Ad-hoc runs"
                    note="Queued with adhoc=true · excluded from the chart and the run list above"
                />
            )}
        </div>
    );
}
