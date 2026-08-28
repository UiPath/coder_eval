import Link from "next/link";
import { notFound } from "next/navigation";
import {
    parseConversation,
    readConversationLog,
    readLogTail,
    readTaskDetail,
    readTaskReplicates,
    replicateDirName,
} from "@/lib/runs";
import { readTaskReview } from "@/lib/reviews";
import { sourceById } from "@/lib/sources";
import { scalarParam, withSource } from "@/app/_lib/source-param";
import { fmtCompact, fmtRunTime, humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";
import { ChipButton } from "../chips";
import { VersionChip } from "@/app/_components/version-list";
import { isInternal } from "@/lib/edition";
import { displayedTurns } from "@/lib/turns";
import { DurationStat, ExpectedTimeStat } from "./task-stats";
import { ExpectedTurnsStat, TurnsStat } from "./turns-stat";
import {
    ArtifactsSection,
    ConversationSection,
    CostExplorerSection,
    CriteriaSection,
    FlowDebugSection,
    LogTailSection,
    ProviderCallTableSection,
    ToolTimelineSection,
} from "./_sections";

export const dynamic = "force-dynamic";

export default async function TaskPage({
    params,
    searchParams,
}: {
    params: Promise<{ id: string; task: string[] }>;
    searchParams: Promise<{ r?: string; src?: string | string[] }>;
}) {
    const { id, task: taskSegments } = await params;
    const { r, src } = await searchParams;
    // Which container this run lives in. Unknown/absent coerces to the skills
    // nightly, so every URL that predates the Scribe tab keeps resolving as-is.
    const source = sourceById(scalarParam(src));
    const taskId = taskSegments.join("/");
    // Replicate index from ?r=NN — repeated runs of one task share this task
    // path, so the query param is what selects which replicate's <NN>/ dir to
    // open. Absent / non-numeric / negative → replicate 0 (the single result a
    // non-repeated or legacy run has).
    const parsedR = Number(r);
    const replicate =
        r != null && Number.isInteger(parsedR) && parsedR >= 0 ? parsedR : 0;
    const task = await readTaskDetail(id, taskId, replicate, source);
    if (!task) notFound();

    // Replicate indices available for this task — drives the run selector below.
    // [0] (or fewer) for a non-repeated task, so the selector self-hides.
    const replicates = await readTaskReplicates(id, taskId, source);

    // variant is always "default" here; the replicate selects the <NN>/ dir.
    // readTaskReview returns null for older runs that predate the review feature.
    const review = await readTaskReview(
        id,
        "default",
        taskId,
        replicateDirName(replicate),
        source,
    );

    const log = await readLogTail(id, taskId, replicate, undefined, source);
    const conversation = parseConversation(
        await readConversationLog(id, taskId, replicate, undefined, source),
    );
    const { flowDebug } = task;

    return (
        <div className="space-y-6">
            <nav className="text-sm text-gray-500 flex items-center gap-2 flex-wrap">
                <Link
                    href={withSource(`/runs/${id}`, source.id)}
                    className="hover:text-studio-blue"
                >
                    ← Run {fmtRunTime(id)}
                </Link>
                <span className="text-gray-300">/</span>
                <span className="font-mono text-gray-700">{taskId}</span>
                {replicates.length > 1 && (
                    <>
                        <span className="text-gray-300">/</span>
                        <span className="font-mono text-gray-700">
                            replicate {replicate}
                        </span>
                    </>
                )}
            </nav>

            {replicates.length > 1 && (
                // Sticky so the run switcher stays reachable while scrolling the
                // (often long) task detail. Negative margins + re-added padding
                // let the white backing span the full content width so sections
                // scroll cleanly underneath. Sits below the header's z-index.
                <div className="sticky top-0 z-20 -mx-4 sm:-mx-6 lg:-mx-8 px-4 sm:px-6 lg:px-8 py-2 bg-white/95 backdrop-blur border-b border-gray-100 flex items-center gap-2 flex-wrap text-sm">
                    <span className="text-gray-500">
                        Runs ({replicates.length}):
                    </span>
                    <div className="inline-flex rounded-md border border-gray-200 overflow-hidden">
                        {replicates.map((ri) => {
                            const active = ri === replicate;
                            return (
                                <Link
                                    key={ri}
                                    href={withSource(
                                        `/runs/${id}/${taskId}?r=${ri}`,
                                        source.id,
                                    )}
                                    // Keep the scroll position when switching runs
                                    // — Next scrolls to top on nav by default.
                                    scroll={false}
                                    aria-current={active ? "page" : undefined}
                                    className={`px-3 py-1 tabular-nums border-r border-gray-200 last:border-r-0 ${
                                        active
                                            ? "bg-studio-blue text-white"
                                            : "bg-white text-gray-700 hover:bg-gray-50"
                                    }`}
                                >
                                    {ri}
                                </Link>
                            );
                        })}
                    </div>
                </div>
            )}

            <div className="space-y-3">
                <div className="flex items-center gap-3 flex-wrap">
                    <h1 className="text-xl font-semibold text-gray-900">
                        {humanizeTaskId(taskId)}
                    </h1>
                    <StatusPill status={task.status} relabel />
                    {/* Download zips the task folder from blob storage — an
                        internal-hosting surface (no blob backend in the public
                        OSS edition). See lib/edition.ts. */}
                    {isInternal && (
                        <a
                            href={withSource(
                                `/api/download?run=${encodeURIComponent(
                                    id,
                                )}&task=${encodeURIComponent(taskId)}`,
                                source.id,
                            )}
                            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 hover:text-studio-blue"
                            download
                        >
                            ↓ Download folder (.zip)
                        </a>
                    )}
                </div>
                <div className="text-xs text-gray-500 tabular-nums font-mono flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
                    <span>
                        {taskId} · run {id}
                        {replicates.length > 1 && ` · replicate ${replicate}`}
                    </span>
                    {/* Component SHAs point at internal tooling; internal-only.
                        See lib/edition.ts. */}
                    {isInternal && task.componentShas.length > 0 && (
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
                <dl className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-4 text-sm bg-gray-50 border border-gray-200 rounded-lg p-4">
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Score
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {task.weightedScore?.toFixed(2) ?? "—"}
                        </dd>
                    </div>
                    <DurationStat
                        durationSeconds={task.durationSeconds}
                        expectedSeconds={task.expectedSeconds}
                    />
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
                    <ExpectedTimeStat expectedSeconds={task.expectedSeconds} />
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
            {task.postFailureCriteria.length > 0 && (
                <CriteriaSection
                    criteria={task.postFailureCriteria}
                    title="Post-failure artifact evidence"
                    diagnostic
                />
            )}
            {conversation.length > 0 && (
                <ConversationSection turns={conversation} />
            )}
            {task.messages.length > 0 && (
                <CostExplorerSection
                    messages={task.messages}
                    subAgentUsageByToolId={task.subAgentUsageByToolId}
                    tokens={task.tokens}
                    recordedCostUsd={task.totalCostUsd}
                />
            )}
            <ProviderCallTableSection providerCalls={task.providerCalls} />
            {(task.toolCalls.length > 0 || task.finalAssistantText) && (
                <ToolTimelineSection
                    toolCalls={task.toolCalls}
                    finalAssistantText={task.finalAssistantText}
                />
            )}
            <ArtifactsSection
                runId={id}
                artifacts={task.artifacts}
                sourceId={source.id}
            />
            <LogTailSection log={log} />
        </div>
    );
}
