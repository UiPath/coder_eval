"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { WINDOWS, type Window } from "@/lib/reviews-types";

export function WindowSelector({ current }: { current: Window }) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const set = (w: Window) => {
        const p = new URLSearchParams(searchParams.toString());
        p.set("window", w);
        router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    };
    return (
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden text-sm">
            {WINDOWS.map((w) => {
                const active = w === current;
                return (
                    <button
                        key={w}
                        type="button"
                        onClick={() => set(w)}
                        aria-pressed={active}
                        className={`px-3 py-1 ${active ? "bg-studio-blue text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}
                    >
                        {w}
                    </button>
                );
            })}
        </div>
    );
}
