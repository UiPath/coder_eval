"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ALL_HARNESSES } from "@/lib/harness";
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
    const segment = (active: boolean) =>
        `flex items-center gap-1.5 px-3 py-1 ${
            active
                ? "bg-studio-blue text-white"
                : "bg-white text-gray-700 hover:bg-gray-50"
        }`;
    return (
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden text-sm">
            {includeAll && (
                <button
                    type="button"
                    onClick={() => set(null)}
                    aria-pressed={current == null}
                    className={segment(current == null)}
                    title="Every harness, one line each"
                >
                    All
                </button>
            )}
            {opts.map((h) => {
                const active = h === current;
                return (
                    <button
                        key={h}
                        type="button"
                        onClick={() => set(h)}
                        aria-pressed={active}
                        className={segment(active)}
                    >
                        <HarnessBadge harness={h} />
                        {harnessShortLabel(h)}
                    </button>
                );
            })}
        </div>
    );
}

// Re-exported so a caller can build an `?h=all` link without importing the leaf
// constants module directly.
export { ALL_HARNESSES };
