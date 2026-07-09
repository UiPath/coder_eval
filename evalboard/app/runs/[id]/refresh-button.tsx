"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";

// Drops this run's cached blob copy via /api/refresh, then soft-refreshes the
// route so the re-downloaded data renders. Use after editing a run's title or
// description in storage — the disk cache otherwise serves the old copy.
export function RefreshButton({ runId }: { runId: string }) {
    const router = useRouter();
    const [pending, startTransition] = useTransition();

    const refresh = () =>
        startTransition(async () => {
            await fetch(`/api/refresh?run=${encodeURIComponent(runId)}`, {
                method: "POST",
            });
            router.refresh();
        });

    return (
        <button
            type="button"
            onClick={refresh}
            disabled={pending}
            title="Re-download this run from storage (use after editing its title or description)"
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 hover:text-studio-blue disabled:opacity-50"
        >
            {pending ? "↻ Refreshing…" : "↻ Refresh"}
        </button>
    );
}
