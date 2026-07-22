import { Suspense } from "react";
import { loadRecentRuns, listRecentHarnesses } from "@/lib/overview";
import { TRENDS_RECENT_RUN_COUNT } from "@/lib/trends";
import { buildWatchlist } from "@/lib/watchlist";
import { KNOWN_HARNESSES, parseHarnessParam } from "@/lib/harness";
import { HarnessSelector } from "@/app/_components/harness-selector";
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
                <div className="ml-auto">
                    <HarnessSelector
                        current={activeHarness}
                        harnesses={KNOWN_HARNESSES}
                    />
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
