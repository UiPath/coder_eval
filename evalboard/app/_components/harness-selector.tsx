"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { harnessColor } from "@/lib/harness";
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
    // Always show the active harness, even if it has aged out of the recent
    // window (so a deep-linked `?h=` still reads as selected rather than absent).
    const opts =
        current == null || harnesses.includes(current)
            ? harnesses
            : [current, ...harnesses];
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
    // The segment's line color on the overview charts. Same swatch as the chart
    // legend, so the control reads as the thing that picks those lines rather
    // than as an unrelated filter. The white ring only shows on the selected
    // segment, where the swatch sits on the blue fill.
    const dot = (h: string, active: boolean) => (
        <span
            aria-hidden
            className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                active ? "ring-1 ring-white/80" : ""
            }`}
            style={{ backgroundColor: harnessColor(h) }}
        />
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
                        {dot(h, active)}
                        <HarnessBadge harness={h} />
                        {label(name)}
                    </button>
                );
            })}
        </div>
    );
}
