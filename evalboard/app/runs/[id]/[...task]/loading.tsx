import {
    SkelBar,
    SkelTile,
    SkeletonPage,
} from "@/app/_components/skeleton";

// Fallback for a single task. 0.7-0.9s warm once the task folder is cached, and
// longer on a cold one: opening a deep link fetches that task's artifacts from
// blob before the page can render.
//
// The shape is a breadcrumb, the task heading, its metric tiles, then the
// transcript panel that fills the rest of the page.
export default function Loading() {
    return (
        <SkeletonPage label="Loading task">
            <div className="space-y-5">
                <div className="space-y-2">
                    <SkelBar className="h-3 w-full max-w-xs" />
                    <SkelBar className="h-7 w-full max-w-lg" />
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <SkelTile />
                    <SkelTile />
                    <SkelTile />
                    <SkelTile />
                </div>
                <SkelTile className="h-96">
                    <div className="space-y-3">
                        <SkelBar className="h-4 w-1/3" />
                        <SkelBar className="h-3 w-full" />
                        <SkelBar className="h-3 w-5/6" />
                        <SkelBar className="h-3 w-full" />
                        <SkelBar className="h-3 w-2/3" />
                    </div>
                </SkelTile>
            </div>
        </SkeletonPage>
    );
}
