import {
    aggregateTaskTrends,
    trendMatchesTag,
    TRENDS_RECENT_RUN_COUNT,
} from "@/lib/trends";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
import { KNOWN_HARNESSES } from "@/app/_components/harness-badge";
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

// The harness whose trend to show. The nightly rotates claude-code / codex /
// antigravity as separate runs; a trend is only meaningful within one harness,
// so the page scopes to one (default claude-code, the daily primary) and offers
// a switcher. An unknown/absent value falls back to the default.
function parseHarness(raw: string | string[] | undefined): string {
    const v = Array.isArray(raw) ? raw[0] : raw;
    return v && (KNOWN_HARNESSES as readonly string[]).includes(v)
        ? v
        : "claude-code";
}

export default async function TrendsPage({
    searchParams,
}: {
    searchParams: Promise<{ q?: string; tag?: string; h?: string }>;
}) {
    const params = await searchParams;
    const q = parseQ(params.q);
    const activeTag = parseTag(params.tag);
    const harness = parseHarness(params.h);

    // One cached load, scoped to the selected harness — yields the trends, the
    // run axis, AND the tag-rail counts (all from the same window).
    const {
        runIds,
        trends: allTrends,
        tagCounts,
    } = await aggregateTaskTrends(TRENDS_RECENT_RUN_COUNT, harness);

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
            // Remount on harness switch so open-row/history state (which is
            // harness-specific) resets cleanly.
            key={harness}
            tasks={tasks}
            runIds={runIds}
            q={q}
            activeTag={activeTag}
            activeHarness={harness}
            harnesses={KNOWN_HARNESSES}
            skills={tagCounts.skills}
            taskTags={tagCounts.taskTags}
            reviewTags={tagCounts.reviewTags}
            provenance={provenance}
        />
    );
}
