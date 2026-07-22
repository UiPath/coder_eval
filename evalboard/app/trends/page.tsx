import { Suspense } from "react";
import {
    aggregateTaskTrends,
    trendMatchesTag,
    TRENDS_RECENT_RUN_COUNT,
} from "@/lib/trends";
import { listRecentHarnesses } from "@/lib/overview";
import { KNOWN_HARNESSES, parseHarnessParam } from "@/lib/harness";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
import { harnessShortLabel } from "@/app/_components/harness-badge";
import { HarnessSelector } from "@/app/_components/harness-selector";
import { TrendsView } from "./trends-view";

export const dynamic = "force-dynamic";

function parseQ(raw: string | string[] | undefined): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return null;
    const trimmed = v.trim();
    return trimmed ? trimmed.slice(0, 200) : null;
}

function parseTag(raw: string | string[] | undefined): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return null;
    const trimmed = v.trim();
    if (!trimmed) return null;
    if (!/^[\w.:/+-]+$/.test(trimmed)) return null;
    return trimmed.slice(0, 100);
}

// The page shell only needs the URL params (cheap), so it returns immediately
// and streams the data-backed table in behind a Suspense boundary — the ~20 MB
// cold aggregate load no longer blocks the whole response. The header +
// harness selector paint instantly (and stay usable) via the fallback.
export default async function TrendsPage({
    searchParams,
}: {
    searchParams: Promise<{ q?: string; tag?: string; h?: string }>;
}) {
    const params = await searchParams;
    const q = parseQ(params.q);
    const activeTag = parseTag(params.tag);
    const harness = parseHarnessParam(params.h);

    return (
        // Key on harness so a switch (a fresh, heavy load) shows the skeleton;
        // q/tag changes keep the boundary and re-filter the cached aggregate in
        // a transition without flashing the fallback.
        <Suspense key={harness} fallback={<TrendsSkeleton activeHarness={harness} />}>
            <TrendsContent q={q} activeTag={activeTag} harness={harness} />
        </Suspense>
    );
}

async function TrendsContent({
    q,
    activeTag,
    harness,
}: {
    q: string | null;
    activeTag: string | null;
    harness: string;
}) {
    // One cached load, scoped to the selected harness — yields the trends, the
    // run axis, AND the tag-rail counts (all from the same window). The harness
    // list (for the switcher) is a separate small cached discovery load.
    const [{ runIds, trends: allTrends, tagCounts }, harnesses] =
        await Promise.all([
            aggregateTaskTrends(TRENDS_RECENT_RUN_COUNT, harness),
            listRecentHarnesses(),
        ]);

    // Provenance: surface the actual run count + date span. Even scoped to one
    // harness, the window can straddle model/prompt regimes, so showing the
    // span keeps the user aware of what's collapsed. Derived from the same
    // cached aggregate as the table (runIds is newest-first), so the label and
    // the strips can't skew apart.
    const provenance =
        runIds.length === 0
            ? null
            : {
                  count: runIds.length,
                  oldest: fmtRunTime(runIds[runIds.length - 1]),
                  newest: fmtRunTime(runIds[0]),
              };

    let tasks = allTrends;
    if (activeTag) {
        tasks = tasks.filter((t) => trendMatchesTag(t, activeTag));
    }
    if (q) {
        const needle = q.toLowerCase();
        tasks = tasks.filter((a) => {
            if (a.taskId.toLowerCase().includes(needle)) return true;
            if (humanizeTaskId(a.taskId).toLowerCase().includes(needle))
                return true;
            if (a.skill && a.skill.toLowerCase().includes(needle)) return true;
            if (a.tags.some((tag) => tag.toLowerCase().includes(needle)))
                return true;
            return a.dominantFailureTags.some((t) =>
                t.tag.toLowerCase().includes(needle),
            );
        });
    }

    return (
        <TrendsView
            tasks={tasks}
            runIds={runIds}
            q={q}
            activeTag={activeTag}
            activeHarness={harness}
            harnesses={harnesses}
            skills={tagCounts.skills}
            taskTags={tagCounts.taskTags}
            reviewTags={tagCounts.reviewTags}
            provenance={provenance}
        />
    );
}

// Streamed while the aggregate loads. Mirrors TrendsView's header (so it
// doesn't jump on swap) and keeps the harness selector live so the user can
// re-scope without waiting, then shims the table with pulsing rows.
function TrendsSkeleton({ activeHarness }: { activeHarness: string }) {
    return (
        <div className="space-y-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-xl font-semibold text-gray-900">
                        Task trends
                    </h1>
                    <p className="text-xs text-gray-500 mt-0.5">
                        Per-task pass rate and averages across recent{" "}
                        {harnessShortLabel(activeHarness)} runs. Averages cover
                        successful runs only.
                    </p>
                </div>
                <HarnessSelector
                    current={activeHarness}
                    harnesses={KNOWN_HARNESSES}
                />
            </div>
            <div className="border border-gray-200 rounded-lg bg-white p-4">
                <div className="h-4 w-40 bg-gray-100 rounded animate-pulse" />
            </div>
            <div
                className="border border-gray-200 rounded-lg overflow-hidden"
                aria-hidden
            >
                {Array.from({ length: 8 }).map((_, i) => (
                    <div
                        key={i}
                        className="h-11 border-b border-gray-100 last:border-0 bg-white flex items-center px-4"
                    >
                        <div className="h-3 w-72 max-w-full bg-gray-100 rounded animate-pulse" />
                    </div>
                ))}
            </div>
        </div>
    );
}
