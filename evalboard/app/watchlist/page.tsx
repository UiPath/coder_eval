import { loadRecentRuns } from "@/lib/overview";
import { TRENDS_RECENT_RUN_COUNT } from "@/lib/trends";
import { buildWatchlist } from "@/lib/watchlist";
import { WatchlistView } from "./watchlist-view";

export const dynamic = "force-dynamic";

export default async function WatchlistPage() {
    const perRun = await loadRecentRuns(TRENDS_RECENT_RUN_COUNT);
    const data = buildWatchlist(perRun);
    return <WatchlistView data={data} />;
}
