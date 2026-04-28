import { notFound } from "next/navigation";
import { readRunSummary, readRunTasks } from "@/lib/runs";
import { fmtDuration, fmtRunTime } from "@/lib/format";
import { TaskGrid } from "./task-grid";

export const dynamic = "force-dynamic";

function Metric({
    label,
    value,
    sub,
    valueClass = "text-gray-900",
}: {
    label: string;
    value: string;
    sub?: string;
    valueClass?: string;
}) {
    return (
        <div className="bg-white border border-gray-200 rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                {label}
            </div>
            <div
                className={`text-2xl font-semibold mt-1 tabular-nums ${valueClass}`}
            >
                {value}
            </div>
            {sub && (
                <div className="text-xs text-gray-500 tabular-nums mt-0.5">
                    {sub}
                </div>
            )}
        </div>
    );
}

export default async function RunPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = await params;
    const [summary, tasks] = await Promise.all([
        readRunSummary(id),
        readRunTasks(id),
    ]);
    if (!summary || !tasks) notFound();

    const total = summary.tasksRun;
    const passed = summary.tasksSucceeded;
    const pct = total ? (passed / total) * 100 : 0;
    const failed = summary.tasksFailed + summary.tasksError;

    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">Run</h1>
                <div className="text-xs text-gray-500 tabular-nums font-mono">
                    {id} · {fmtRunTime(id)}
                </div>
                {summary.componentShas.length > 0 && (
                    <div className="text-xs text-gray-500 font-mono pt-1 flex flex-wrap gap-x-3 gap-y-1">
                        {summary.componentShas.map((c) => (
                            <span key={c.name}>
                                <span className="text-gray-400">{c.name}:</span>{" "}
                                {c.url ? (
                                    <a
                                        href={c.url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="text-blue-600 hover:underline"
                                    >
                                        {c.sha}
                                    </a>
                                ) : (
                                    <span className="text-gray-400">
                                        {c.sha}
                                    </span>
                                )}
                            </span>
                        ))}
                    </div>
                )}
            </div>

            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div className="col-span-2 bg-white border border-gray-200 rounded-lg p-4">
                    <div className="text-xs text-gray-500 uppercase tracking-wide">
                        Pass rate
                    </div>
                    <div className="flex items-baseline gap-2 mt-1">
                        <span className="text-2xl font-semibold text-gray-900 tabular-nums">
                            {pct.toFixed(0)}%
                        </span>
                        <span className="text-sm text-gray-500 tabular-nums">
                            {passed} / {total}
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
                    sub={
                        summary.tasksError
                            ? `${summary.tasksFailed} fail · ${summary.tasksError} error`
                            : undefined
                    }
                    valueClass={failed > 0 ? "text-red-700" : "text-gray-900"}
                />
                <Metric
                    label="Total cost"
                    value={
                        summary.totalCostUsd != null
                            ? `$${summary.totalCostUsd.toFixed(2)}`
                            : "—"
                    }
                />
                <Metric
                    label="Total duration"
                    value={fmtDuration(summary.durationSeconds)}
                />
            </div>

            <div className="flex items-baseline gap-3 pt-2">
                <h2 className="text-sm font-semibold text-gray-900">Tasks</h2>
                <span className="text-xs text-gray-500">
                    {tasks.length} total · click to open
                </span>
            </div>
            <TaskGrid runId={id} tasks={tasks} />
        </div>
    );
}
