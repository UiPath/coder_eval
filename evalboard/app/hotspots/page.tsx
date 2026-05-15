import { aggregateRecentTasks, WINDOWS, type Window } from "@/lib/reviews";
import { humanizeTaskId } from "@/lib/format";
import { HotspotsView } from "./hotspots-view";

export const dynamic = "force-dynamic";

function parseWindow(raw: string | string[] | undefined): Window {
    const v = Array.isArray(raw) ? raw[0] : raw;
    return WINDOWS.includes(v as Window) ? (v as Window) : "7d";
}

function parseQ(raw: string | string[] | undefined): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (!v) return null;
    const trimmed = v.trim();
    return trimmed ? trimmed.slice(0, 200) : null;
}

export default async function HotspotsPage({
    searchParams,
}: {
    searchParams: Promise<{ window?: string; q?: string }>;
}) {
    const params = await searchParams;
    const window = parseWindow(params.window);
    const q = parseQ(params.q);
    const all = await aggregateRecentTasks(window);
    const tasks = q
        ? (() => {
              const needle = q.toLowerCase();
              return all.filter((a) => {
                  if (a.taskId.toLowerCase().includes(needle)) return true;
                  if (humanizeTaskId(a.taskId).toLowerCase().includes(needle))
                      return true;
                  return a.dominantTags.some((t) =>
                      t.tag.toLowerCase().includes(needle),
                  );
              });
          })()
        : all;
    return <HotspotsView window={window} tasks={tasks} q={q} totalCount={all.length} />;
}
