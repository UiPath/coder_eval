"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

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
    // True while the user has typed ahead of the last URL write. Prevents the
    // URL-sync effect from overwriting in-progress input when a navigation
    // triggered by the debounce resolves asynchronously.
    const typingAhead = useRef(false);

    // Sync local state when the URL changes externally (back/forward, link
    // clicks). Skipped while the user is ahead of the URL to avoid clobbering
    // in-progress input with a stale value from a just-resolved navigation.
    useEffect(() => {
        if (typingAhead.current) return;
        setQ((prev) => (prev.trim() === urlQ ? prev : urlQ));
    }, [urlQ]);

    useEffect(() => {
        const trimmed = q.trim();
        if (trimmed === urlQ) {
            typingAhead.current = false;
            return;
        }
        typingAhead.current = true;
        const timer = setTimeout(() => {
            typingAhead.current = false;
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
        // Don't clear typingAhead in cleanup — the timer was cancelled because
        // the user typed another character, so they're still ahead of the URL.
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
