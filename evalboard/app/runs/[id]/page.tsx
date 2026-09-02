import { Suspense } from "react";
import { notFound } from "next/navigation";
import {
    findMatureSourceRuns,
    readActivationScore,
    readRunAnalysis,
    readRunMeta,
    readRunSummary,
    readRunTasks,
} from "@/lib/runs";
import { readRunReviewIndex, indexByTask, tagCountsForRun } from "@/lib/reviews";
import { sourceById } from "@/lib/sources";
import { scalarParam } from "@/app/_lib/source-param";
import { fmtRunTime } from "@/lib/format";
import { AnalysisPanel } from "./analysis-panel";
import { RefreshButton } from "./refresh-button";
import { RunView } from "./run-view";
import { RunIdentity } from "./run-identity";
import { VersionList } from "@/app/_components/version-list";
import { isInternal } from "@/lib/edition";

export const dynamic = "force-dynamic";

export default async function RunPage({
    params,
    searchParams,
}: {
    params: Promise<{ id: string }>;
    searchParams: Promise<{ src?: string | string[] }>;
}) {
    const { id } = await params;
    // Which container this run lives in. Unknown/absent coerces to the skills
    // nightly, so every URL that predates the Scribe tab keeps resolving as-is.
    const source = sourceById(scalarParam((await searchParams).src));
    const [summary, tasks, activation, analysis, reviewIndex, meta] =
        await Promise.all([
            readRunSummary(id, source),
            readRunTasks(id, source),
            // Nested activation sub-run rollup (null when the run has no
            // activation suite); drives the clickable activation metric card.
            readActivationScore(id, source),
            readRunAnalysis(id, source),
            readRunReviewIndex(id, source),
            readRunMeta(id, source),
        ]);
    if (!summary || !tasks) notFound();
    // Mature-skipped tasks weren't executed this run, so they have no detail page
    // here — resolve the most recent earlier run where each one did run, so the
    // grid can link the row out to that execution. Empty (no extra reads) when
    // the run carried no mature tasks forward.
    //
    // The look-back scan reads up to MATURE_SOURCE_LOOKBACK earlier run.jsons,
    // any of which can throw on a transient blob/auth/IMDS error. The primary
    // run already loaded above, and this affordance is purely decorative (it
    // only makes mature rows clickable), so a scan failure degrades to the
    // existing non-clickable-row fallback instead of crashing the whole page.
    let matureSourceRuns: Record<string, string> = {};
    try {
        matureSourceRuns = await findMatureSourceRuns(
            // Dedupe: a replicated task contributes one row per replicate, so the
            // same mature task_id can appear N times — pass each id once.
            [
                ...new Set(
                    tasks.filter((t) => t.matureSkipped).map((t) => t.taskId),
                ),
            ],
            id,
            undefined,
            source,
        );
    } catch (err) {
        console.error(`findMatureSourceRuns failed for run ${id}:`, err);
    }
    const tagCounts = reviewIndex ? tagCountsForRun(reviewIndex) : [];
    const reviewsByTask = reviewIndex ? indexByTask(reviewIndex) : undefined;
    const human = fmtRunTime(id);

    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                    <h1 className="text-xl font-semibold text-gray-900">
                        {meta?.title ?? "Run"}
                    </h1>
                    {meta?.adhoc && (
                        <span className="text-[10px] uppercase tracking-wide font-semibold text-amber-700 bg-amber-100 border border-amber-200 rounded px-1.5 py-0.5">
                            Ad-hoc
                        </span>
                    )}
                    {/* Refresh re-pulls the run from blob storage — an
                        internal-hosting surface (the public OSS edition has no
                        blob backend). See lib/edition.ts.

                        There is deliberately no whole-run download here. A
                        nightly run is ~10k blobs / ~400 MB, and zipping it meant
                        fetching every one of them with no concurrency cap,
                        walking the tree with a stat per file over Azure Files,
                        and buffering the entire archive in memory before the
                        first byte reached the browser — minutes of apparent
                        hang, against a process that already peaks at 2.7 GB RSS.
                        Per-task download (on the task page) is the supported
                        shape; it bundles a handful of files and returns in
                        well under a second. */}
                    {isInternal && (
                        <div className="ml-auto flex items-center gap-2">
                            <RefreshButton runId={id} sourceId={source.id} />
                        </div>
                    )}
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
                {/* Harness + model, so the page says what produced these
                    numbers without a trip back to the runs table. */}
                <RunIdentity
                    harness={summary.harness}
                    model={summary.model}
                    modelCount={summary.modelCount}
                />
                {/* Component SHAs (cli / agent / sdk / drawer …) point at
                    internal tooling; internal-only. See lib/edition.ts. */}
                {isInternal && (
                    <VersionList versions={summary.componentShas} />
                )}
            </div>

            {analysis && <AnalysisPanel markdown={analysis} />}

            <Suspense fallback={null}>
                <RunView
                    runId={id}
                    tasks={tasks}
                    activation={activation}
                    reviewsByTask={reviewsByTask}
                    reviewTagCounts={tagCounts}
                    matureSourceRuns={matureSourceRuns}
                    isInternal={isInternal}
                    sourceId={source.id}
                />
            </Suspense>
        </div>
    );
}
