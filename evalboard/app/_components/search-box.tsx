"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

const Q_DEBOUNCE_MS = 300;

export function SearchBox({
    className = "",
    placeholder = "Search runs, tasks, or tags…",
}: {
    className?: string;
    placeholder?: string;
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();

    const urlQ = searchParams.get("q") ?? "";
    const [q, setQ] = useState(urlQ);

    // Sync local state when the URL changes externally (back/forward, link
    // clicks). The debounced write below early-returns when state and URL
    // agree, so this can't loop.
    useEffect(() => {
        setQ((prev) => (prev.trim() === urlQ ? prev : urlQ));
    }, [urlQ]);

    useEffect(() => {
        const trimmed = q.trim();
        if (trimmed === urlQ) return;
        const timer = setTimeout(() => {
            // Read the live URL at fire time so a concurrent write (e.g. a
            // tag click that landed during the debounce) isn't clobbered.
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
                placeholder={placeholder}
                aria-label="Search"
                className="w-full text-sm border border-gray-200 rounded-md pl-4 pr-9 py-2 focus:outline-none focus:border-studio-blue focus:ring-1 focus:ring-studio-blue"
            />
            {q && (
                <button
                    type="button"
                    onClick={() => setQ("")}
                    aria-label="Clear search"
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
                >
                    ×
                </button>
            )}
        </div>
    );
}
