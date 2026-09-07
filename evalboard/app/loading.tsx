import {
    SkelBar,
    SkelCards,
    SkelTable,
    SkelTile,
    SkeletonPage,
} from "@/app/_components/skeleton";

// Fallback for the front page and, by nesting, for every route without a
// closer one: /trends, /watchlist, /path-to-ga, /scribe and /runs/latest.
// Warm server time on those ranges from 3ms (/watchlist) to 2.3s
// (/path-to-ga), and the slow ones are slow because they read run.json off the
// Azure Files mount, which is worse in production than locally.
//
// The shape is the one those pages share: a heading, a row of stat tiles, a
// wide panel (chart or prose), then a listing. Two tile columns on a phone and
// four from md, matching the real grids.
export default function Loading() {
    return (
        <SkeletonPage label="Loading page">
            <div className="space-y-5">
                <div className="space-y-2">
                    <SkelBar className="h-7 w-40" />
                    <SkelBar className="h-4 w-full max-w-md" />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <SkelTile />
                    <SkelTile />
                    <SkelTile />
                    <SkelTile />
                </div>
                <SkelTile className="h-48">
                    <SkelBar className="h-full w-full" />
                </SkelTile>
                <SkelTable className="hidden md:block" rows={8} />
                <SkelCards className="md:hidden" count={5} />
            </div>
        </SkeletonPage>
    );
}
