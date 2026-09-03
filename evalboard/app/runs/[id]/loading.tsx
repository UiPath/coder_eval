import {
    RunBodySkeleton,
    SkelBar,
    SkeletonPage,
} from "@/app/_components/skeleton";

// Fallback for a run and its activation sub-page. This is the slowest route in
// the app: 1.7-2.2s warm on a 1,296-task run, because the server parses a
// multi-MB run.json, the activation sub-run, the review index and the
// mature-source scan before it can emit anything.
//
// Covers a client navigation into the run, where nothing of the page exists
// yet, so it draws the header too. Once the header has streamed, the grid's own
// boundary in page.tsx takes over with the same body skeleton.
export default function Loading() {
    return (
        <SkeletonPage label="Loading run">
            <div className="space-y-5">
                <div className="space-y-1.5">
                    <div className="flex items-center gap-2">
                        <SkelBar className="h-7 w-20" />
                        <SkelBar className="ml-auto h-8 w-24 shrink-0" />
                    </div>
                    <SkelBar className="h-4 w-full max-w-xl" />
                    <SkelBar className="h-4 w-2/3 max-w-sm" />
                </div>
                <RunBodySkeleton />
            </div>
        </SkeletonPage>
    );
}
