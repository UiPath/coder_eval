// "What am I looking at" strip for the run header. The run page used to identify
// a run only by its timestamped id, so telling a codex run from a claude-code one
// meant going back to the runs table (which does carry a Harness column) or
// opening a task. Harness and model are the two facts that decide whether a
// number on this page is comparable to a number on another run's page, so they
// belong in the header.

import {
    HarnessBadge,
    harnessShortLabel,
} from "@/app/_components/harness-badge";

export function RunIdentity({
    harness,
    model,
    modelCount,
}: {
    harness: string | null;
    model: string | null;
    modelCount: number;
}) {
    // Legacy runs identify neither; render nothing rather than an empty frame.
    if (!harness && !model) return null;
    return (
        <div className="flex flex-wrap items-center gap-2 pt-1.5">
            {harness && (
                <span
                    className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-gray-50 px-2 py-1 text-xs font-medium text-gray-800"
                    title="Harness this run executed on"
                >
                    <HarnessBadge harness={harness} size={16} />
                    {harnessShortLabel(harness)}
                </span>
            )}
            {model && (
                <span
                    className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-800"
                    title={
                        modelCount > 1
                            ? `${modelCount} models across this run's tasks; showing the most common`
                            : "Model this run's tasks ran on"
                    }
                >
                    <span className="text-gray-400">model</span>
                    <span className="font-mono">{model}</span>
                    {modelCount > 1 && (
                        <span className="text-gray-500">
                            +{modelCount - 1} more
                        </span>
                    )}
                </span>
            )}
        </div>
    );
}
