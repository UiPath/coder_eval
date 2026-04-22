import Link from "next/link";
import { notFound } from "next/navigation";
import { readLogTail, readTaskDetail } from "@/lib/runs";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";
import {
    ArtifactsSection,
    CriteriaSection,
    FlowDebugSection,
    LogTailSection,
    ToolTimelineSection,
} from "./_sections";

export const dynamic = "force-dynamic";

export default async function TaskPage({
    params,
}: {
    params: Promise<{ id: string; task: string }>;
}) {
    const { id, task: taskId } = await params;
    const task = await readTaskDetail(id, taskId);
    if (!task) notFound();

    const log = await readLogTail(id, taskId);
    const { flowDebug } = task;

    return (
        <div className="space-y-6">
            <nav className="text-sm text-gray-500 flex items-center gap-2 flex-wrap">
                <Link href={`/runs/${id}`} className="hover:text-studio-blue">
                    ← Run {fmtRunTime(id)}
                </Link>
                <span className="text-gray-300">/</span>
                <span className="font-mono text-gray-700">{taskId}</span>
            </nav>

            <div className="space-y-3">
                <div className="flex items-center gap-3 flex-wrap">
                    <h1 className="text-xl font-semibold text-gray-900">
                        {humanizeTaskId(taskId)}
                    </h1>
                    <StatusPill status={task.status} relabel />
                    {flowDebug?.studioWebUrl && (
                        <a
                            href={flowDebug.studioWebUrl}
                            target="_blank"
                            rel="noreferrer"
                            className="ml-auto inline-flex items-center gap-1.5 text-sm bg-studio-blue hover:bg-studio-blue-hover text-white px-3 py-1.5 rounded-md transition-colors"
                        >
                            Open in Studio Web
                            <span className="text-xs">↗</span>
                        </a>
                    )}
                </div>
                <div className="text-xs text-gray-500 tabular-nums font-mono">
                    {taskId} · run {id}
                </div>
                <dl className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm bg-gray-50 border border-gray-200 rounded-lg p-4">
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Score
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.weightedScore?.toFixed(2) ?? "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Duration
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.durationSeconds
                                ? `${task.durationSeconds.toFixed(1)}s`
                                : "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Cost
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.totalCostUsd != null
                                ? `$${task.totalCostUsd.toFixed(3)}`
                                : "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Final status
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5">
                            {task.finalStatus ?? "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Tool calls
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.toolCalls.length}
                        </dd>
                    </div>
                </dl>
                {task.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                        {task.tags.map((t) => (
                            <span
                                key={t}
                                className="text-xs text-gray-600 bg-gray-100 border border-gray-200 px-2 py-0.5 rounded"
                            >
                                {t}
                            </span>
                        ))}
                    </div>
                )}
            </div>

            {task.taskDescription && (
                <section className="space-y-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Prompt
                    </h2>
                    <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 border border-gray-200 rounded-lg p-4 font-sans leading-relaxed">
                        {task.taskDescription}
                    </pre>
                </section>
            )}

            {task.errorMessage && (
                <div className="border border-red-200 bg-red-50 rounded-lg p-3 text-sm text-red-700 whitespace-pre-wrap">
                    {task.errorMessage}
                </div>
            )}

            {flowDebug && <FlowDebugSection flowDebug={flowDebug} />}
            <CriteriaSection criteria={task.criteria} />
            {task.toolCalls.length > 0 && (
                <ToolTimelineSection toolCalls={task.toolCalls} />
            )}
            <ArtifactsSection runId={id} artifacts={task.artifacts} />
            <LogTailSection log={log} />
        </div>
    );
}
