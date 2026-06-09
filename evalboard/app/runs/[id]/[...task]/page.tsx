import Link from "next/link";
import { notFound } from "next/navigation";
import { readLogTail, readTaskDetail } from "@/lib/runs";
import { readTaskReview } from "@/lib/reviews";
import { fmtCompact, fmtRunTime, humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";
import { ChipButton } from "../chips";
import { VersionChip } from "@/app/_components/version-list";
import { displayedTurns } from "@/lib/turns";
import { ExpectedTurnsStat, TurnsStat } from "./turns-stat";
import {
    ArtifactsSection,
    CostExplorerSection,
    CriteriaSection,
    FlowDebugSection,
    LogTailSection,
    ToolTimelineSection,
} from "./_sections";

export const dynamic = "force-dynamic";

export default async function TaskPage({
    params,
}: {
    params: Promise<{ id: string; task: string[] }>;
}) {
    const { id, task: taskSegments } = await params;
    const taskId = taskSegments.join("/");
    const task = await readTaskDetail(id, taskId);
    if (!task) notFound();

    // The rest of this page already assumes variant="default" and replicate
    // "00" (mirrors readTaskDetail / ensureTaskDir). readTaskReview returns
    // null for older runs that predate the review feature.
    const review = await readTaskReview(id, "default", taskId, "00");

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
                    <a
                        href={`/api/download?run=${encodeURIComponent(
                            id,
                        )}&task=${encodeURIComponent(taskId)}`}
                        className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 hover:text-studio-blue"
                        download
                    >
                        ↓ Download folder (.zip)
                    </a>
                </div>
                <div className="text-xs text-gray-500 tabular-nums font-mono flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
                    <span>
                        {taskId} · run {id}
                    </span>
                    {task.componentShas.length > 0 && (
                        <details className="group inline-block">
                            <summary className="flex items-center gap-1 cursor-pointer list-none [&::-webkit-details-marker]:hidden hover:text-gray-700">
                                <span className="text-gray-300">·</span>
                                <span>versions</span>
                                <span
                                    aria-hidden
                                    className="inline-block transition-transform group-open:rotate-90"
                                >
                                    ▸
                                </span>
                            </summary>
                            <div className="flex flex-wrap gap-x-3 gap-y-1 pt-1.5">
                                {task.componentShas.map((c) => (
                                    <VersionChip key={c.name} {...c} />
                                ))}
                            </div>
                        </details>
                    )}
                </div>
                <dl className="grid grid-cols-2 md:grid-cols-7 gap-4 text-sm bg-gray-50 border border-gray-200 rounded-lg p-4">
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
                    <TurnsStat
                        turns={displayedTurns(
                            task.actualCommands,
                            task.hasFinalReply,
                        )}
                        expectedTurns={task.expectedTurns}
                    />
                    <ExpectedTurnsStat expectedTurns={task.expectedTurns} />
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Tokens
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.tokens.total > 0
                                ? fmtCompact(task.tokens.total)
                                : "—"}
                        </dd>
                        {task.tokens.total > 0 && (
                            <dd
                                className="text-[10px] text-gray-500 mt-0.5 tabular-nums"
                                title="in (uncached input) · out (output) · cw (cache-creation input) · cr (cache-read input)"
                            >
                                in {fmtCompact(task.tokens.input)} · out {fmtCompact(task.tokens.output)} · cw {fmtCompact(task.tokens.cacheCreation)} · cr {fmtCompact(task.tokens.cacheRead)}
                            </dd>
                        )}
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

            {review && (
                <section className="space-y-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Review
                    </h2>
                    <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 space-y-2">
                        <p className="text-sm text-gray-800 leading-relaxed">
                            {review.summary}
                        </p>
                        {review.tags.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-0.5">
                                {review.tags.map((t) => (
                                    <ChipButton
                                        key={t}
                                        tag={t}
                                        variant="review"
                                        size="sm"
                                        active={false}
                                    />
                                ))}
                            </div>
                        )}
                    </div>
                </section>
            )}

            {flowDebug && <FlowDebugSection flowDebug={flowDebug} />}
            <CriteriaSection criteria={task.criteria} />
            {task.messages.length > 0 && (
                <CostExplorerSection
                    messages={task.messages}
                    subAgentUsageByToolId={task.subAgentUsageByToolId}
                    tokens={task.tokens}
                    recordedCostUsd={task.totalCostUsd}
                />
            )}
            {(task.toolCalls.length > 0 || task.finalAssistantText) && (
                <ToolTimelineSection
                    toolCalls={task.toolCalls}
                    finalAssistantText={task.finalAssistantText}
                />
            )}
            <ArtifactsSection runId={id} artifacts={task.artifacts} />
            <LogTailSection log={log} />
        </div>
    );
}
