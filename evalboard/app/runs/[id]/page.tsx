import type { ReactNode } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { readLogTail, readRunDetail } from "@/lib/runs";
import { humanizeTaskId } from "@/lib/format";
import { StatusPill } from "@/lib/pills";

export const dynamic = "force-dynamic";

function Expandable({
    header,
    children,
}: {
    header: ReactNode;
    children: ReactNode;
}) {
    return (
        <details className="group border border-gray-200 rounded-lg bg-white overflow-hidden">
            <summary className="px-3 py-2 hover:bg-gray-50 flex items-center gap-1 cursor-pointer transition-colors list-none [&::-webkit-details-marker]:hidden">
                <span
                    aria-hidden="true"
                    className="inline-block w-4 text-gray-400 transition-transform group-open:rotate-90"
                >
                    ▶
                </span>
                <span className="flex-1">{header}</span>
            </summary>
            <div className="px-3 pb-3 pt-1 border-t border-gray-100">
                {children}
            </div>
        </details>
    );
}

function ResultPill({ passed }: { passed: boolean }) {
    const cls = passed
        ? "bg-green-50 text-green-700 border-green-200"
        : "bg-red-50 text-red-700 border-red-200";
    return (
        <span
            className={`inline-block px-2 py-0.5 text-xs rounded-full border font-medium ${cls}`}
        >
            {passed ? "PASS" : "FAIL"}
        </span>
    );
}

function KindChip({ kind }: { kind: string }) {
    const isFlow = kind === "flow";
    const cls = isFlow
        ? "bg-orange-50 text-uipath-orange border-orange-200"
        : "bg-gray-50 text-gray-700 border-gray-200";
    return (
        <span
            className={`inline-block text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${cls}`}
        >
            {kind}
        </span>
    );
}

function ToolChip({ tool }: { tool: string }) {
    // Stable per-name tint — keep it subtle.
    const map: Record<string, string> = {
        Bash: "bg-gray-100 text-gray-700 border-gray-200",
        Read: "bg-blue-50 text-blue-700 border-blue-200",
        Write: "bg-purple-50 text-purple-700 border-purple-200",
        Edit: "bg-purple-50 text-purple-700 border-purple-200",
        Grep: "bg-teal-50 text-teal-700 border-teal-200",
        Glob: "bg-teal-50 text-teal-700 border-teal-200",
        Skill: "bg-orange-50 text-uipath-orange border-orange-200",
        ToolSearch: "bg-gray-50 text-gray-500 border-gray-200",
    };
    const cls = map[tool] ?? "bg-gray-50 text-gray-700 border-gray-200";
    return (
        <span
            className={`inline-block text-[10px] font-medium px-1.5 py-0.5 rounded border ${cls}`}
        >
            {tool}
        </span>
    );
}

export default async function RunPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = await params;
    const run = await readRunDetail(id);
    if (!run) notFound();

    const log = run.taskSubdir ? await readLogTail(id, run.taskSubdir) : "";
    const { flowDebug } = run;

    return (
        <div className="space-y-6">
            <div>
                <Link
                    href="/"
                    className="text-sm text-gray-500 hover:text-studio-blue"
                >
                    ← All runs
                </Link>
            </div>

            <div className="space-y-3">
                <div className="flex items-center gap-3 flex-wrap">
                    <h1 className="text-xl font-semibold text-gray-900">
                        {humanizeTaskId(run.taskId)}
                    </h1>
                    <StatusPill status={run.status} relabel />
                    {flowDebug?.studioWebUrl && (
                        <a
                            href={flowDebug.studioWebUrl}
                            target="_blank"
                            rel="noreferrer"
                            className="ml-auto inline-flex items-center gap-1.5 text-sm bg-studio-blue hover:bg-studio-blue-hover text-white px-3 py-1.5 rounded-md transition-colors"
                        >
                            Open in Studio Web
                            <span className="text-xs">↗</span>
                        </a>
                    )}
                </div>
                <div className="text-xs text-gray-500 tabular-nums font-mono">
                    {run.taskId} · {run.id}
                </div>
                <dl className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm bg-gray-50 border border-gray-200 rounded-lg p-4">
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Score
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {run.weightedScore?.toFixed(2) ?? "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Duration
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {run.durationSeconds
                                ? `${run.durationSeconds.toFixed(1)}s`
                                : "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Cost
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {run.totalCostUsd != null
                                ? `$${run.totalCostUsd.toFixed(3)}`
                                : "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Final status
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5">
                            {run.finalStatus ?? "—"}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-xs text-gray-500 uppercase tracking-wide">
                            Tool calls
                        </dt>
                        <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                            {run.toolCalls.length}
                        </dd>
                    </div>
                </dl>
                {run.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                        {run.tags.map((t) => (
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

            {run.taskDescription && (
                <section className="space-y-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Prompt
                    </h2>
                    <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 border border-gray-200 rounded-lg p-4 font-sans leading-relaxed">
                        {run.taskDescription}
                    </pre>
                </section>
            )}

            {run.errorMessage && (
                <div className="border border-red-200 bg-red-50 rounded-lg p-3 text-sm text-red-700 whitespace-pre-wrap">
                    {run.errorMessage}
                </div>
            )}

            {flowDebug && (
                <section className="space-y-2">
                    <div className="flex items-center gap-3">
                        <h2 className="text-sm font-semibold text-gray-900">
                            Flow debug elements ({flowDebug.elements.length})
                        </h2>
                        {flowDebug.finalStatus && (
                            <StatusPill status={flowDebug.finalStatus} />
                        )}
                    </div>
                    {flowDebug.elements.length === 0 ? (
                        <div className="text-sm text-gray-500">
                            no element executions
                        </div>
                    ) : (
                        <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                        <th className="py-2 px-3 font-medium">
                                            Element
                                        </th>
                                        <th className="py-2 px-3 font-medium">
                                            Type
                                        </th>
                                        <th className="py-2 px-3 font-medium">
                                            Status
                                        </th>
                                        <th className="py-2 px-3 font-medium">
                                            Output / error
                                        </th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {flowDebug.elements.map((e) => (
                                        <tr
                                            key={e.elementId}
                                            className="border-b border-gray-100 last:border-b-0"
                                        >
                                            <td className="py-2 px-3 font-medium text-gray-900 font-mono text-xs">
                                                {e.elementId}
                                            </td>
                                            <td className="py-2 px-3 text-gray-500 text-xs">
                                                {e.elementType ?? "—"}
                                            </td>
                                            <td className="py-2 px-3">
                                                <StatusPill
                                                    status={e.status}
                                                />
                                            </td>
                                            <td className="py-2 px-3 text-xs text-gray-700 font-mono break-all">
                                                {e.errorMessage ? (
                                                    <span className="text-red-700">
                                                        {e.errorMessage}
                                                    </span>
                                                ) : (
                                                    (e.outputPreview ?? "—")
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </section>
            )}

            <section className="space-y-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    Success criteria ({run.criteria.length})
                </h2>
                <div className="space-y-2">
                    {run.criteria.map((c, i) => {
                        const passed = c.score === 1;
                        return (
                            <Expandable
                                key={i}
                                header={
                                    <div className="flex items-center gap-3">
                                        <ResultPill passed={passed} />
                                        <span className="text-sm text-gray-900">
                                            {c.description ??
                                                c.criterionType ??
                                                `criterion ${i + 1}`}
                                        </span>
                                        <span className="ml-auto text-xs text-gray-500 tabular-nums">
                                            score {c.score ?? "—"}
                                        </span>
                                    </div>
                                }
                            >
                                {c.details && (
                                    <pre className="whitespace-pre-wrap text-xs text-gray-800 bg-gray-50 p-3 rounded border border-gray-200 overflow-x-auto font-mono">
                                        {c.details}
                                    </pre>
                                )}
                                {c.error && (
                                    <pre className="whitespace-pre-wrap text-xs text-red-700 bg-red-50 p-3 rounded border border-red-200 mt-2 overflow-x-auto font-mono">
                                        {c.error}
                                    </pre>
                                )}
                            </Expandable>
                        );
                    })}
                    {run.criteria.length === 0 && (
                        <div className="text-sm text-gray-500">
                            no criteria recorded
                        </div>
                    )}
                </div>
            </section>

            {run.toolCalls.length > 0 && (
                <section className="space-y-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Command timeline ({run.toolCalls.length})
                    </h2>
                    <Expandable
                        header={
                            <span className="text-sm text-gray-700">
                                agent tool calls in order
                            </span>
                        }
                    >
                        <ol className="space-y-1 text-xs font-mono">
                            {run.toolCalls.map((t) => (
                                <li
                                    key={t.index}
                                    className="flex items-start gap-2 py-0.5"
                                >
                                    <span className="text-gray-400 tabular-nums w-6 text-right shrink-0">
                                        {t.index}.
                                    </span>
                                    <span className="shrink-0">
                                        <ToolChip tool={t.tool} />
                                    </span>
                                    <span className="text-gray-700 truncate">
                                        {t.summary}
                                    </span>
                                </li>
                            ))}
                        </ol>
                    </Expandable>
                </section>
            )}

            <section className="space-y-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    Artifacts
                </h2>
                {run.artifacts.length === 0 && (
                    <div className="text-sm text-gray-500">none</div>
                )}
                <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg bg-white">
                    {run.artifacts.map((a) => (
                        <li
                            key={a.relPath}
                            className="flex items-center gap-3 px-3 py-2 text-sm"
                        >
                            <KindChip kind={a.kind} />
                            <a
                                href={`/api/file?run=${encodeURIComponent(
                                    run.id,
                                )}&path=${encodeURIComponent(a.relPath)}`}
                                className="text-studio-blue hover:underline truncate"
                                download
                            >
                                {a.relPath}
                            </a>
                            <span className="ml-auto text-xs text-gray-500 tabular-nums">
                                {(a.sizeBytes / 1024).toFixed(1)} KB
                            </span>
                        </li>
                    ))}
                </ul>
            </section>

            <section className="space-y-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    task.log
                </h2>
                <Expandable
                    header={
                        <span className="text-sm text-gray-700">
                            {log.length.toLocaleString()} bytes
                            <span className="text-gray-400">
                                {" "}
                                · click to view
                            </span>
                        </span>
                    }
                >
                    <pre className="whitespace-pre-wrap text-xs text-gray-800 bg-gray-50 p-3 rounded border border-gray-200 overflow-x-auto max-h-[600px] overflow-y-auto font-mono">
                        {log}
                    </pre>
                </Expandable>
            </section>
        </div>
    );
}
