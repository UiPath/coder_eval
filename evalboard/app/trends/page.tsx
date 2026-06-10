import {
    aggregateTaskTrends,
    trendMatchesTag,
    TRENDS_RECENT_RUN_COUNT,
} from "@/lib/trends";
import { aggregateTaskTagCounts, loadRecentRuns } from "@/lib/overview";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
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

export default async function TrendsPage({
    searchParams,
}: {
    searchParams: Promise<{ q?: string; tag?: string }>;
}) {
    const params = await searchParams;
    const q = parseQ(params.q);
    const activeTag = parseTag(params.tag);

    const [{ runIds, trends: allTrends }, perRun] = await Promise.all([
        aggregateTaskTrends(TRENDS_RECENT_RUN_COUNT),
        loadRecentRuns(TRENDS_RECENT_RUN_COUNT),
    ]);
    const tagCounts = aggregateTaskTagCounts(perRun);

    // Provenance: surface the actual run count + date span. Mixing runs across
    // model/agent regimes into a single pass rate is otherwise invisible —
    // showing the window at least makes the user aware of what's collapsed.
    // Derived from the same cached aggregate as the table (runIds is
    // newest-first), so the label and the strips can't skew apart.
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
            skills={tagCounts.skills}
            taskTags={tagCounts.taskTags}
            reviewTags={tagCounts.reviewTags}
            provenance={provenance}
        />
    );
}
