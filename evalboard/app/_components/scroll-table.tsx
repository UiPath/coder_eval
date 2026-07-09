"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Horizontal-scroll wrapper for wide tables. On narrow screens (small laptops,
// phones) a `w-full` table or fixed-width grid used to overflow its
// `overflow-hidden` box and silently CLIP the right-hand columns — the run grid
// showed only Task + Status on a phone. This scrolls instead, and paints a fade
// on whichever edge still has hidden content so it's obvious more is there.
//
// The fades are absolutely-positioned overlays (not the CSS background-attachment
// trick) so they sit ON TOP of opaque cell backgrounds and stay correct with a
// sticky first column.
export function TableScroll({
    children,
    footer,
    className = "",
}: {
    children: React.ReactNode;
    // Optional pinned row (e.g. a "show more" bar) rendered inside the border but
    // outside the horizontal scroll area, so it stays full-width.
    footer?: React.ReactNode;
    className?: string;
}) {
    const ref = useRef<HTMLDivElement>(null);
    const [atStart, setAtStart] = useState(true);
    const [atEnd, setAtEnd] = useState(true);

    const measure = useCallback(() => {
        const el = ref.current;
        if (!el) return;
        const max = el.scrollWidth - el.clientWidth;
        // 1px slack so sub-pixel rounding doesn't leave a fade stuck on.
        setAtStart(el.scrollLeft <= 1);
        setAtEnd(el.scrollLeft >= max - 1);
    }, []);

    useEffect(() => {
        const el = ref.current;
        if (!el) return;
        measure();
        // ResizeObserver is absent in jsdom (tests) and very old browsers —
        // fall back to the window resize listener alone there.
        const ro =
            typeof ResizeObserver !== "undefined"
                ? new ResizeObserver(measure)
                : null;
        if (ro) {
            ro.observe(el);
            // Children width can change without the scroller resizing (e.g. a
            // collapsible column group), so observe the inner content too.
            if (el.firstElementChild) ro.observe(el.firstElementChild);
        }
        window.addEventListener("resize", measure);
        return () => {
            ro?.disconnect();
            window.removeEventListener("resize", measure);
        };
    }, [measure]);

    return (
        <div
            className={
                "relative border border-gray-200 rounded-lg bg-white overflow-hidden " +
                className
            }
        >
            <div ref={ref} onScroll={measure} className="overflow-x-auto">
                {children}
            </div>
            {/* The pinned footer renders after the fades and is opaque, so it
                paints over their bottom edge — no need to size the fades around it. */}
            {!atStart && (
                <div
                    aria-hidden
                    className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-gray-900/10 to-transparent"
                />
            )}
            {!atEnd && (
                <div
                    aria-hidden
                    className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-gray-900/10 to-transparent"
                />
            )}
            {footer && <div className="relative">{footer}</div>}
        </div>
    );
}
