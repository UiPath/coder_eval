"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { HarnessBadge, harnessShortLabel } from "./harness-badge";

// Segmented control for the trends page's harness scope. Sets `?h=<harness>`
// while preserving the active q/tag params, mirroring WindowSelector. Each
// segment shows the vendor logo + short label so it reads like the per-run
// harness badge on the runs tables.
export function HarnessSelector({
    current,
    harnesses,
}: {
    current: string;
    harnesses: readonly string[];
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const set = (h: string) => {
        const p = new URLSearchParams(searchParams.toString());
        p.set("h", h);
        router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    };
    // Always show the active harness, even if it has aged out of the recent
    // window (so a deep-linked `?h=` still reads as selected rather than absent).
    const opts = (harnesses as readonly string[]).includes(current)
        ? harnesses
        : [current, ...harnesses];
    return (
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden text-sm">
            {opts.map((h) => {
                const active = h === current;
                return (
                    <button
                        key={h}
                        type="button"
                        onClick={() => set(h)}
                        aria-pressed={active}
                        className={`flex items-center gap-1.5 px-3 py-1 ${active ? "bg-studio-blue text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}
                    >
                        <HarnessBadge harness={h} />
                        {harnessShortLabel(h)}
                    </button>
                );
            })}
        </div>
    );
}
