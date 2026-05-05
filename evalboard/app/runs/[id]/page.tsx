import { Suspense } from "react";
import { notFound } from "next/navigation";
import { readRunAnalysis, readRunSummary, readRunTasks } from "@/lib/runs";
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
    const [summary, tasks, analysis] = await Promise.all([
        readRunSummary(id),
        readRunTasks(id),
        readRunAnalysis(id),
    ]);
    if (!summary || !tasks) notFound();

    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">Run</h1>
                <div className="text-xs text-gray-500 tabular-nums font-mono">
                    {id} · {fmtRunTime(id)}
                </div>
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
                <RunView runId={id} tasks={tasks} />
            </Suspense>
        </div>
    );
}
