// Mobile height-clamp for the tag rail. On a phone the rail is a wall of ~70
// chips that buries everything below it; on a laptop it's a tidy couple of rows.
// So on small screens we clamp it to ~2 rows with a fade and a "Show all tags"
// toggle, and on md+ we let it render in full. Pure CSS (a peer checkbox), so it
// stays a server component alongside the rail it wraps.
//
// `id` must be unique on the page — pass a distinct one per rail instance.
export function CollapsibleRail({
    id,
    children,
}: {
    id: string;
    children: React.ReactNode;
}) {
    return (
        <div className="relative">
            <input id={id} type="checkbox" className="peer sr-only" />
            <div className="max-h-16 overflow-hidden peer-checked:max-h-none md:max-h-none">
                {children}
            </div>
            {/* Fade over the last row of the clamped rail. Pulled up over the
                content with a negative margin; hidden once expanded or on md+. */}
            <div
                aria-hidden
                className="pointer-events-none relative -mt-8 h-8 bg-gradient-to-t from-white to-transparent peer-checked:hidden md:hidden"
            />
            {/* Two labels, each a direct sibling of the peer input so
                peer-checked toggles which one shows (a nested span wouldn't be a
                sibling, so the variant wouldn't reach it). */}
            <label
                htmlFor={id}
                className="md:hidden peer-checked:hidden mt-1 inline-flex cursor-pointer items-center gap-1 rounded border border-dashed border-gray-300 px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors"
            >
                Show all tags
            </label>
            <label
                htmlFor={id}
                className="hidden peer-checked:inline-flex md:!hidden mt-1 cursor-pointer items-center gap-1 rounded border border-dashed border-gray-300 px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors"
            >
                Show less
            </label>
        </div>
    );
}
