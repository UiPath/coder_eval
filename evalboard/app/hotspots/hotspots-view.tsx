"use client";

import Link from "next/link";
import { useCallback, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
    type TaskAggregate,
    type TaskRunHit,
    type Window,
    WINDOWS,
} from "@/lib/reviews-types";
import { fmtRunTime, humanizeTaskId } from "@/lib/format";
import { fetchTaskDrilldownAction } from "./actions";

function WindowSelector({ current }: { current: Window }) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const set = (w: Window) => {
        const p = new URLSearchParams(searchParams.toString());
        p.set("window", w);
        router.replace(`${pathname}?${p.toString()}`, { scroll: false });
    };
    return (
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden text-sm">
            {WINDOWS.map((w) => {
                const active = w === current;
                return (
                    <button
                        key={w}
                        type="button"
                        onClick={() => set(w)}
                        aria-pressed={active}
                        className={`px-3 py-1 ${active ? "bg-studio-blue text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}
                    >
                        {w}
                    </button>
                );
            })}
        </div>
    );
}

function TagChip({ tag, count }: { tag: string; count: number }) {
    return (
        <span className="inline-flex items-center gap-1 text-[10px] leading-none px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200">
            <span>{tag}</span>
            <span className="tabular-nums text-rose-400">{count}</span>
        </span>
    );
}

function TaskRow({
    a,
    expanded,
    drilldown,
    pending,
    onToggle,
}: {
    a: TaskAggregate;
    expanded: boolean;
    drilldown: TaskRunHit[] | null;
    pending: boolean;
    onToggle: () => void;
}) {
    return (
        <>
            <tr
                onClick={onToggle}
                className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer"
            >
                <td className="py-2 px-4">
                    <div className="font-medium text-gray-900">
                        {humanizeTaskId(a.taskId)}
                    </div>
                    <div className="text-[11px] text-gray-500 font-mono">
                        {a.taskId}
                    </div>
                </td>
                <td className="py-2 px-4 tabular-nums text-right">
                    {a.affectedRuns}
                </td>
                <td className="py-2 px-4 tabular-nums text-right">
                    {a.occurrences}
                </td>
                <td className="py-2 px-4">
                    <div className="flex flex-wrap gap-1">
                        {a.dominantTags.map((t) => (
                            <TagChip
                                key={t.tag}
                                tag={t.tag}
                                count={t.count}
                            />
                        ))}
                    </div>
                </td>
                <td className="py-2 px-4 text-xs text-gray-500 tabular-nums whitespace-nowrap">
                    {fmtRunTime(a.lastSeenRunId)}
                </td>
                <td className="py-2 px-2 text-xs text-gray-400">
                    {expanded ? "▾" : "▸"}
                </td>
            </tr>
            {expanded && (
                <tr className="bg-gray-50 border-b border-gray-100">
                    <td colSpan={6} className="px-4 py-3">
                        {pending && !drilldown ? (
                            <span className="text-xs text-gray-500">
                                Loading…
                            </span>
                        ) : drilldown == null ? (
                            <span className="text-xs text-gray-500">
                                No data.
                            </span>
                        ) : drilldown.length === 0 ? (
                            <span className="text-xs text-gray-500">
                                No reviews for this task in the window.
                            </span>
                        ) : (
                            <ul className="space-y-1.5 text-xs">
                                {drilldown.map((h, i) => (
                                    <li
                                        key={`${h.runId}/${h.replicate}/${i}`}
                                        className="flex flex-wrap gap-x-2 gap-y-0.5 items-baseline"
                                    >
                                        <Link
                                            href={`/runs/${h.runId}/${a.taskId}`}
                                            className="text-studio-blue hover:underline font-mono"
                                        >
                                            {h.runId}
                                        </Link>
                                        {h.replicate && h.replicate !== "00" && (
                                            <span className="text-gray-400 font-mono">
                                                rep {h.replicate}
                                            </span>
                                        )}
                                        {h.tags.length > 0 && (
                                            <span className="flex flex-wrap gap-1">
                                                {h.tags.map((t) => (
                                                    <span
                                                        key={t}
                                                        className="text-[10px] leading-none px-1 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200"
                                                    >
                                                        {t}
                                                    </span>
                                                ))}
                                            </span>
                                        )}
                                        <span className="text-gray-600">
                                            {h.summaryExcerpt}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </td>
                </tr>
            )}
        </>
    );
}

export function HotspotsView({
    window,
    tasks,
}: {
    window: Window;
    tasks: TaskAggregate[];
}) {
    const [openTaskId, setOpenTaskId] = useState<string | null>(null);
    const [drill, setDrill] = useState<Record<string, TaskRunHit[]>>({});
    const [pending, startTransition] = useTransition();

    const toggle = useCallback(
        (taskId: string) => {
            setOpenTaskId((cur) => (cur === taskId ? null : taskId));
            if (drill[taskId] != null) return;
            startTransition(async () => {
                const hits = await fetchTaskDrilldownAction(taskId, window);
                setDrill((prev) => ({ ...prev, [taskId]: hits }));
            });
        },
        [drill, window],
    );

    return (
        <div className="space-y-5">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div>
                    <h1 className="text-xl font-semibold text-gray-900">
                        Hotspots
                    </h1>
                    <p className="text-xs text-gray-500 mt-0.5">
                        Tasks with the most failed reviews in the window. Click
                        a row to see the runs and tags behind it.
                    </p>
                </div>
                <WindowSelector current={window} />
            </div>

            {tasks.length === 0 ? (
                <div className="bg-white border border-gray-200 rounded-lg p-6 text-center text-sm text-gray-500">
                    No review data in the last {window}.
                </div>
            ) : (
                <div className="border border-gray-200 rounded-lg overflow-hidden bg-white">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                                <th className="py-2 px-4 font-medium">Task</th>
                                <th className="py-2 px-4 font-medium text-right">
                                    Runs
                                </th>
                                <th className="py-2 px-4 font-medium text-right">
                                    Occurrences
                                </th>
                                <th className="py-2 px-4 font-medium">
                                    Tags
                                </th>
                                <th className="py-2 px-4 font-medium">
                                    Last seen
                                </th>
                                <th className="py-2 px-2"></th>
                            </tr>
                        </thead>
                        <tbody>
                            {tasks.map((a) => (
                                <TaskRow
                                    key={a.taskId}
                                    a={a}
                                    expanded={openTaskId === a.taskId}
                                    drilldown={drill[a.taskId] ?? null}
                                    pending={pending && openTaskId === a.taskId}
                                    onToggle={() => toggle(a.taskId)}
                                />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}
