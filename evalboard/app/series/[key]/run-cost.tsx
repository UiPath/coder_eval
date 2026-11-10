import { fmtUsd } from "@/lib/format";
import type { SeriesDef } from "@/lib/series";
import { loadRunLevel } from "@/lib/series-data";

export async function RunCost({ def, id }: { def: SeriesDef; id: string }) {
    const level = await loadRunLevel(def, id);
    return <>{level?.totalCostUsd != null ? fmtUsd(level.totalCostUsd) : "—"}</>;
}
