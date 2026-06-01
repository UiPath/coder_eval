import { Suspense } from "react";
import { notFound } from "next/navigation";
import {
    readRunAnalysis,
    readRunMeta,
    readRunSummary,
    readRunTasks,
} from "@/lib/runs";
import { readRunReviewIndex, indexByTask, tagCountsForRun } from "@/lib/reviews";
import { fmtRunTime } from "@/lib/format";
import { AnalysisPanel } from "./analysis-panel";
import { RunView } from "./run-view";

export const dynamic = "force-dynamic";

export default async function RunPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = await params;
    const [summary, tasks, analysis, reviewIndex, meta] = await Promise.all([
        readRunSummary(id),
        readRunTasks(id),
        readRunAnalysis(id),
        readRunReviewIndex(id),
        readRunMeta(id),
    ]);
    if (!summary || !tasks) notFound();
    const tagCounts = reviewIndex ? tagCountsForRun(reviewIndex) : [];
    const reviewsByTask = reviewIndex ? indexByTask(reviewIndex) : undefined;
    const human = fmtRunTime(id);

    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <div className="flex items-center gap-2">
                    <h1 className="text-xl font-semibold text-gray-900">
                        {meta?.title ?? "Run"}
                    </h1>
                    {meta?.adhoc && (
                        <span className="text-[10px] uppercase tracking-wide font-semibold text-amber-700 bg-amber-100 border border-amber-200 rounded px-1.5 py-0.5">
                            Ad-hoc
                        </span>
                    )}
                    <a
                        href={`/api/download?run=${encodeURIComponent(id)}`}
                        className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 hover:text-studio-blue"
                        download
                    >
                        ↓ Download run (.zip)
                    </a>
                </div>
                <div className="text-xs text-gray-500 tabular-nums font-mono">
                    {id}
                    {human !== id && ` · ${human}`}
                </div>
                {meta?.description && (
                    <p className="text-sm text-gray-600 whitespace-pre-wrap pt-1">
                        {meta.description}
                    </p>
                )}
                {summary.componentShas.length > 0 && (
                    <div className="text-xs text-gray-500 font-mono pt-1 flex flex-wrap gap-x-3 gap-y-1">
                        {summary.componentShas.map((c) => (
                            <span key={c.name}>
                                <span className="text-gray-400">{c.name}:</span>{" "}
                                {c.url ? (
                                    <a
                                        href={c.url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="text-blue-600 hover:underline"
                                    >
                                        {c.sha}
                                    </a>
                                ) : (
                                    <span className="text-gray-400">
                                        {c.sha}
                                    </span>
                                )}
                            </span>
                        ))}
                    </div>
                )}
            </div>

            {analysis && <AnalysisPanel markdown={analysis} />}

            <Suspense fallback={null}>
                <RunView
                    runId={id}
                    tasks={tasks}
                    reviewsByTask={reviewsByTask}
                    reviewTagCounts={tagCounts}
                />
            </Suspense>
        </div>
    );
}
