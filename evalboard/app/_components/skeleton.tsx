// Shared skeleton primitives for the route-level `loading.tsx` files.
//
// These exist because every heavy route here is force-dynamic: on navigation
// Next has to run the server component before it can render anything, and with
// no Suspense boundary the browser sits on the PREVIOUS page for the whole of
// it. Measured warm, that is 1.4-1.6s on the front page and 1.7-2.2s on a
// 1,296-task run, with no indication the click registered.
//
// Blocks are sized to the element they stand in for and mirror the same
// breakpoints, so the page doesn't jump when the content lands. Nothing here is
// pixel-exact; it only has to read as "this shape, still loading".
//
// Widths are fractional or capped so a phone never scrolls sideways, and the
// pulse is on the wrapper rather than each block: one animated element instead
// of dozens, all in phase. `motion-reduce` drops it entirely.

// One grey block. `className` carries the size, so callers read as a layout.
export function SkelBar({ className = "" }: { className?: string }) {
    return <div className={`rounded bg-gray-100 ${className}`} />;
}

// A bordered card matching the stat tiles and panels: same border, radius and
// padding, so the skeleton occupies the height the real tile will.
export function SkelTile({
    className = "",
    children,
}: {
    className?: string;
    children?: React.ReactNode;
}) {
    return (
        <div
            className={`bg-white border border-gray-200 rounded-lg p-4 ${className}`}
        >
            {children ?? (
                <div className="space-y-2">
                    <SkelBar className="h-3 w-1/2" />
                    <SkelBar className="h-6 w-2/3" />
                </div>
            )}
        </div>
    );
}

// A run of chip-shaped blocks, for the filter rails. Widths vary so it reads as
// text of different lengths rather than a progress bar. `flex-wrap` keeps it on
// screen at any width.
export function SkelChips({ count = 8 }: { count?: number }) {
    // Deterministic, so the server and client markup agree during hydration.
    const widths = ["w-16", "w-24", "w-20", "w-28", "w-14", "w-24", "w-20", "w-32"];
    return (
        <div className="flex flex-wrap gap-1.5">
            {Array.from({ length: count }, (_, i) => (
                <SkelBar
                    key={i}
                    className={`h-5 ${widths[i % widths.length]}`}
                />
            ))}
        </div>
    );
}

// Table stand-in for the md-and-up layout: a header strip plus n body rows,
// inside the same bordered, clipped box the real tables use.
export function SkelTable({
    rows = 8,
    className = "",
}: {
    rows?: number;
    className?: string;
}) {
    return (
        <div
            className={`border border-gray-200 rounded-lg bg-white overflow-hidden ${className}`}
        >
            <div className="bg-gray-50 border-b border-gray-200 px-4 py-2.5">
                <SkelBar className="h-3 w-1/3" />
            </div>
            <div className="divide-y divide-gray-100">
                {Array.from({ length: rows }, (_, i) => (
                    <div
                        key={i}
                        className="px-4 py-3 flex items-center gap-3"
                    >
                        <SkelBar className="h-3.5 flex-1" />
                        <SkelBar className="hidden sm:block h-3.5 w-16 shrink-0" />
                        <SkelBar className="h-3.5 w-10 shrink-0" />
                    </div>
                ))}
            </div>
        </div>
    );
}

// Card stand-in for the below-md layout the grid switches to on a phone.
export function SkelCards({
    count = 5,
    className = "",
}: {
    count?: number;
    className?: string;
}) {
    return (
        <div className={`space-y-2 ${className}`}>
            {Array.from({ length: count }, (_, i) => (
                <div
                    key={i}
                    className="border border-gray-200 rounded-lg bg-white p-3 space-y-2.5"
                >
                    <SkelBar className="h-4 w-3/4" />
                    <div className="grid grid-cols-3 gap-2">
                        <SkelBar className="h-3" />
                        <SkelBar className="h-3" />
                        <SkelBar className="h-3" />
                    </div>
                </div>
            ))}
        </div>
    );
}

// The body of a run page: stat tiles, filter rail, then the task listing. Used
// twice, which is why it lives here rather than in a loading.tsx: once as the
// route fallback (below a skeleton header) and once as the fallback for the
// Suspense boundary the run page wraps its grid in, where the real header has
// already streamed and only this hole is left to fill.
//
// The tile row and the listing mirror the real breakpoints exactly: two tile
// columns on a phone with the pass rate spanning both, five from md; a table at
// md and up, cards below it. Matching them is the point, so the layout doesn't
// move when the content lands.
export function RunBodySkeleton() {
    return (
        <div className="space-y-5">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <SkelTile className="col-span-2" />
                <SkelTile />
                <SkelTile />
                <SkelTile />
            </div>
            <div className="space-y-2">
                <SkelBar className="h-3 w-24" />
                <SkelChips count={8} />
            </div>
            <SkelTable className="hidden md:block" rows={10} />
            <SkelCards className="md:hidden" count={6} />
        </div>
    );
}

// Wrapper every loading.tsx returns. Carries the pulse, and announces itself
// once to a screen reader instead of leaving the blocks as unlabelled noise.
export function SkeletonPage({
    label,
    children,
}: {
    label: string;
    children: React.ReactNode;
}) {
    return (
        <div
            role="status"
            aria-busy="true"
            aria-label={label}
            className="animate-pulse motion-reduce:animate-none"
        >
            <span className="sr-only">{label}</span>
            {children}
        </div>
    );
}
