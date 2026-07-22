import { Suspense } from "react";
import { loadRecentRuns, listRecentHarnesses } from "@/lib/overview";
import { TRENDS_RECENT_RUN_COUNT } from "@/lib/trends";
import { buildWatchlist } from "@/lib/watchlist";
import { KNOWN_HARNESSES, parseHarnessParam } from "@/lib/harness";
import { HarnessSelector } from "@/app/_components/harness-selector";
import { harnessShortLabel } from "@/app/_components/harness-badge";
import { WatchlistView } from "./watchlist-view";

export const dynamic = "force-dynamic";

export default async function WatchlistPage({
    searchParams,
}: {
    searchParams: Promise<{ h?: string }>;
}) {
    const params = await searchParams;
    const harness = parseHarnessParam(params.h);
    return (
        <Suspense
            key={harness}
            fallback={<WatchlistSkeleton activeHarness={harness} />}
        >
            <WatchlistContent harness={harness} />
        </Suspense>
    );
}

async function WatchlistContent({ harness }: { harness: string }) {
    // Scope the whole watchlist to one harness — the pass rates, streaks, and
    // volatility only mean something within a single harness (mixing them makes
    // a Claude-reliable skill look flaky on codex/antigravity days).
    const [perRun, harnesses] = await Promise.all([
        loadRecentRuns(TRENDS_RECENT_RUN_COUNT, harness),
        listRecentHarnesses(),
    ]);
    const data = buildWatchlist(perRun);
    return (
        <WatchlistView
            data={data}
            activeHarness={harness}
            harnesses={harnesses}
        />
    );
}

function WatchlistSkeleton({ activeHarness }: { activeHarness: string }) {
    return (
        <div>
            <div className="flex items-center gap-3 flex-wrap">
                <h1 className="text-[22px] font-bold text-gray-900 border-l-[3px] border-uipath-orange pl-2.5">
                    Watchlist
                </h1>
                {/* Mirror WatchlistView's header exactly — selector + the
                    "last N … runs" pill — so nothing shifts horizontally when
                    the real content streams in and the pill appears. Only the
                    run count is unknown at skeleton time, so it pulses. */}
                <div className="ml-auto flex items-center gap-3">
                    <HarnessSelector
                        current={activeHarness}
                        harnesses={KNOWN_HARNESSES}
                    />
                    <span className="text-[11px] text-gray-600 bg-gray-100 px-3 py-1 rounded-full font-semibold whitespace-nowrap">
                        last{" "}
                        <span className="inline-block w-4 h-3 rounded bg-gray-200 animate-pulse align-middle" />{" "}
                        {harnessShortLabel(activeHarness)} runs
                    </span>
                </div>
            </div>
            <p className="text-gray-500 text-[13px] mt-1.5 mb-6">
                What leadership should be watching — ranked by where the signal
                says to look first, for the selected harness.
            </p>
            <div
                className="bg-white border border-gray-200 rounded-lg p-5 space-y-3"
                aria-hidden
            >
                {Array.from({ length: 5 }).map((_, i) => (
                    <div
                        key={i}
                        className="h-6 bg-gray-100 rounded animate-pulse"
                    />
                ))}
            </div>
        </div>
    );
}
