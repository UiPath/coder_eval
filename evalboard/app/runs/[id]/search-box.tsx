"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

const Q_DEBOUNCE_MS = 300;

export function SearchBox({ className = "" }: { className?: string }) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();

    const urlQ = searchParams.get("q") ?? "";
    const [q, setQ] = useState(urlQ);

    // Sync local state when the URL changes externally (back/forward, manual
    // edit). The debounced write below early-returns once URL and state
    // agree, so this doesn't loop.
    useEffect(() => {
        setQ((prev) => (prev.trim() === urlQ ? prev : urlQ));
    }, [urlQ]);

    useEffect(() => {
        const trimmed = q.trim();
        if (trimmed === urlQ) return;
        const timer = setTimeout(() => {
            // Read the live URL at fire time so a tag click that landed
            // during the debounce window isn't clobbered by a stale snapshot.
            const params = new URLSearchParams(window.location.search);
            if (trimmed) params.set("q", trimmed);
            else params.delete("q");
            const qs = params.toString();
            router.replace(qs ? `${pathname}?${qs}` : pathname, {
                scroll: false,
            });
        }, Q_DEBOUNCE_MS);
        return () => clearTimeout(timer);
    }, [q, urlQ, pathname, router]);

    return (
        <div className={`relative ${className}`}>
            <input
                type="text"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search task or tag…"
                aria-label="Search tasks or tags"
                className="w-full text-sm border border-gray-200 rounded-md px-3 py-1.5 pr-7 focus:outline-none focus:border-studio-blue focus:ring-1 focus:ring-studio-blue"
            />
            {q && (
                <button
                    type="button"
                    onClick={() => setQ("")}
                    aria-label="Clear search"
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
                >
                    ×
                </button>
            )}
        </div>
    );
}
