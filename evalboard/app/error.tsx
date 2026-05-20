"use client";

// Root error boundary. Anything thrown from a server or client component below
// the layout lands here instead of Next.js's framework-default error screen.
// loadPerRunForId already absorbs per-blob failures; this catches the
// upstream-list class (listRunIds auth/IMDS errors) and anything we haven't
// hardened yet.

import { useEffect } from "react";

export default function GlobalError({
    error,
    reset,
}: {
    error: Error & { digest?: string };
    reset: () => void;
}) {
    useEffect(() => {
        console.error("[evalboard] page error:", error);
    }, [error]);

    return (
        <div className="bg-white border border-gray-200 rounded-lg p-6 max-w-xl mx-auto mt-12 space-y-3">
            <h1 className="text-lg font-semibold text-gray-900">
                Something went wrong loading this page.
            </h1>
            <p className="text-sm text-gray-600">
                A run blob or the run listing failed to load. This is usually
                transient.
            </p>
            {error.digest && (
                <p className="text-xs text-gray-400 font-mono">
                    digest: {error.digest}
                </p>
            )}
            <div className="pt-2">
                <button
                    onClick={() => reset()}
                    className="px-3 py-1.5 text-sm rounded-md bg-studio-blue text-white hover:opacity-90"
                >
                    Retry
                </button>
            </div>
        </div>
    );
}
