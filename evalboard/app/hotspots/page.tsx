import { aggregateRecentTasks, WINDOWS, type Window } from "@/lib/reviews";
import { HotspotsView } from "./hotspots-view";

export const dynamic = "force-dynamic";

function parseWindow(raw: string | string[] | undefined): Window {
    const v = Array.isArray(raw) ? raw[0] : raw;
    return WINDOWS.includes(v as Window) ? (v as Window) : "7d";
}

export default async function HotspotsPage({
    searchParams,
}: {
    searchParams: Promise<{ window?: string }>;
}) {
    const params = await searchParams;
    const window = parseWindow(params.window);
    const tasks = await aggregateRecentTasks(window);
    return <HotspotsView window={window} tasks={tasks} />;
}
