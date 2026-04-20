import { listAllRuns } from "@/lib/runs";
import { RunsTable } from "./runs-table";

export const dynamic = "force-dynamic";

function fmtDurTotal(s: number): string {
    if (s < 60) return `${s.toFixed(0)}s`;
    const m = Math.floor(s / 60);
    const rem = Math.round(s - m * 60);
    if (m < 60) return `${m}m ${rem}s`;
    const h = Math.floor(m / 60);
    const mRem = m - h * 60;
    return `${h}h ${mRem}m`;
}

export default async function Page() {
    const runs = await listAllRuns();
    const passed = runs.filter((r) => r.status === "SUCCESS").length;
    const failed = runs.filter(
        (r) => r.status === "FAILURE" || r.status === "ERROR",
    ).length;
    const pct = runs.length ? (passed / runs.length) * 100 : 0;
    const totalCost = runs.reduce((a, r) => a + (r.totalCostUsd ?? 0), 0);
    const totalDur = runs.reduce((a, r) => a + (r.durationSeconds ?? 0), 0);
    const avgDur = runs.length ? totalDur / runs.length : 0;
    return (
        <div className="space-y-5">
            <div className="flex items-baseline gap-3">
                <h1 className="text-xl font-semibold text-gray-900">Runs</h1>
                <span className="text-sm text-gray-500">
                    {runs.length} total
                </span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
                <div className="col-span-2 md:col-span-2 bg-white border border-gray-200 rounded-lg p-4">
                    <div className="text-xs text-gray-500 uppercase tracking-wide">
                        Pass rate
                    </div>
                    <div className="flex items-baseline gap-2 mt-1">
                        <span className="text-2xl font-semibold text-gray-900 tabular-nums">
                            {pct.toFixed(0)}%
                        </span>
                        <span className="text-sm text-gray-500 tabular-nums">
                            {passed} / {runs.length}
                        </span>
                    </div>
                    <div className="mt-3 h-2 bg-gray-100 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-green-500"
                            style={{ width: `${pct}%` }}
                        />
                    </div>
                </div>
                <Metric
                    label="Failed"
                    value={String(failed)}
                    valueClass="text-red-700"
                />
                <Metric
                    label="Total cost"
                    value={`$${totalCost.toFixed(2)}`}
                />
                <Metric
                    label="Total duration"
                    value={fmtDurTotal(totalDur)}
                />
                <Metric label="Avg duration" value={fmtDurTotal(avgDur)} />
            </div>

            <RunsTable runs={runs} />
        </div>
    );
}

function Metric({
    label,
    value,
    valueClass = "text-gray-900",
}: {
    label: string;
    value: string;
    valueClass?: string;
}) {
    return (
        <div className="bg-white border border-gray-200 rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                {label}
            </div>
            <div className={`text-2xl font-semibold mt-1 tabular-nums ${valueClass}`}>
                {value}
            </div>
        </div>
    );
}
