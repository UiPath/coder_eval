"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { KNOWN_HARNESSES, orderHarnesses } from "@/lib/harness";
import { HarnessBadge, harnessShortLabel } from "./harness-badge";

// Segmented control for a page's harness scope. Sets `?h=<harness>` while
// preserving the active q/tag params, mirroring WindowSelector. Each segment
// shows the vendor logo + short label so it reads like the per-run harness badge
// on the runs tables.
//
// `current` is null only on pages that support the all-harness view (the
// overview, whose charts draw a line per harness and whose tiles and run list
// then cover every harness). Pages that need exactly one harness — trends
// collapses per-task history across runs, which only means something inside one
// harness — pass a concrete harness and leave `includeAll` off.
export function HarnessSelector({
    current,
    harnesses,
    includeAll = false,
}: {
    current: string | null;
    harnesses: readonly string[];
    includeAll?: boolean;
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const set = (h: string | null) => {
        const p = new URLSearchParams(searchParams.toString());
        if (h == null) {
            // The unscoped view is the default, so it's the absence of the param
            // rather than `h=all` — keeps the canonical URL clean.
            p.delete("h");
        } else {
            p.set("h", h);
        }
        // Changing scope changes which runs are in play, so the paged-out row
        // counts no longer describe the new set; drop them back to page one.
        p.delete("limit");
        p.delete("alimit");
        const qs = p.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    };
    // Every known harness always gets a segment, whether or not it turned up in
    // the discovery window. A weekly harness (delegate) drops out of that window
    // between firings, and a control that quietly loses an option reads as "this
    // harness was removed" rather than "it hasn't run lately". Scoping to one
    // with no runs in the window shows an empty result, which is honest and one
    // click from recoverable; a missing segment is neither. `current` is unioned
    // in too, so a deep-linked `?h=` outside the known set still reads as
    // selected. orderHarnesses fixes the order, so segments never reshuffle.
    const opts = orderHarnesses([
        ...KNOWN_HARNESSES,
        ...harnesses,
        ...(current == null ? [] : [current]),
    ]);
    // shrink-0 + whitespace-nowrap: a segment must keep its label on one line
    // and never compress, so on a narrow screen the control scrolls (see the
    // wrapper) instead of wrapping "Claude Code" onto two lines and stretching
    // the whole page past the viewport.
    const segment = (active: boolean) =>
        `flex shrink-0 items-center gap-1.5 whitespace-nowrap px-2.5 py-1 sm:px-3 ${
            active
                ? "bg-studio-blue text-white"
                : "bg-white text-gray-700 hover:bg-gray-50"
        }`;
    // Below `sm` the vendor logo carries the identity on its own (each button
    // keeps the full name as its accessible label and tooltip) — five spelled-out
    // segments don't fit on a phone, and the chart legend below names every line.
    const label = (text: string) => (
        <span className="hidden sm:inline">{text}</span>
    );
    return (
        <div className="flex max-w-full overflow-x-auto rounded-md border border-gray-200 text-sm">
            {includeAll && (
                <button
                    type="button"
                    onClick={() => set(null)}
                    aria-pressed={current == null}
                    className={`${segment(current == null)} rounded-l-md`}
                    title="Every harness, one line each"
                >
                    All
                </button>
            )}
            {opts.map((h, i) => {
                const active = h === current;
                const name = harnessShortLabel(h);
                return (
                    <button
                        key={h}
                        type="button"
                        onClick={() => set(h)}
                        aria-pressed={active}
                        aria-label={name}
                        title={name}
                        className={`${segment(active)} ${
                            !includeAll && i === 0 ? "rounded-l-md" : ""
                        } ${i === opts.length - 1 ? "rounded-r-md" : ""}`}
                    >
                        <HarnessBadge harness={h} />
                        {label(name)}
                    </button>
                );
            })}
        </div>
    );
}
