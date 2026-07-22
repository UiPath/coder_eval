import type { ReactNode } from "react";
import { DELIVERABLE_KINDS } from "@/lib/artifact-kinds";

export function Expandable({
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

// `gating: false` (weight: 0) criteria are informational — they cannot fail the
// task, so rendering PASS/FAIL for them would contradict the task's own status.
export function ResultPill({
    passed,
    gating = true,
}: {
    passed: boolean;
    gating?: boolean;
}) {
    const cls = !gating
        ? "bg-gray-50 text-gray-600 border-gray-200"
        : passed
          ? "bg-green-50 text-green-700 border-green-200"
          : "bg-red-50 text-red-700 border-red-200";
    return (
        <span
            className={`inline-block px-2 py-0.5 text-xs rounded-full border font-medium ${cls}`}
        >
            {!gating ? "INFO" : passed ? "PASS" : "FAIL"}
        </span>
    );
}

export function KindChip({ kind }: { kind: string }) {
    const cls = DELIVERABLE_KINDS.has(kind)
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

export function ToolChip({ tool }: { tool: string }) {
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
