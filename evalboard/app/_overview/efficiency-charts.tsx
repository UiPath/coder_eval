"use client";

// The overview's efficiency section, tabbed.
//
// Two signals run side by side for now. "Time" is the derived wall-clock line
// (lib/timing.ts): expected seconds per task per harness, computed from that
// task's own passing history by the eval runner and stamped into run.json.
// "Turns" is the hand-written predecessor it is meant to replace, kept visible
// while the new number is being watched rather than trusted.
//
// The tab strip is the seam: retiring the turn budget is deleting the "turns"
// entry from TABS and the TurnBudgetChart import, with no other edit to this
// page. Tab state is local — deliberately not a search param, so switching
// charts never re-renders the server page or perturbs a shared link.

import { useState } from "react";
import type { RunPoint } from "@/lib/overview";
import { TimePerPassedTaskChart } from "./wall-clock-chart";
import { TurnBudgetChart } from "./turn-budget-chart";

type TabKey = "time" | "turns";

interface ChartProps {
    data: RunPoint[];
    harnesses: string[];
    windowStart: number;
    windowEnd: number;
}

const TABS: Array<{
    key: TabKey;
    label: string;
    heading: string;
    // Hover text on the heading: what the number actually is.
    title: string;
    // Shown under the heading. `scoped` appends the active-filter note.
    blurb: (scoped: boolean) => string;
    render: (props: ChartProps) => React.ReactNode;
}> = [
    {
        key: "time",
        label: "Time",
        heading: "Time per Passed Task",
        title: "Total wall clock ÷ tasks passed. Every task's seconds count, failures included; only passes count in the denominator, so a run that fails more reads slower.",
        blurb: (scoped) =>
            "Seconds of every task that ran ÷ the number that passed · hover a point for the share within 2× expected" +
            (scoped ? " · scoped to the active filter" : ""),
        render: (props) => <TimePerPassedTaskChart {...props} />,
    },
    {
        key: "turns",
        label: "Turns",
        heading: "Within Expected Turns (%)",
        title: "Share of tasks carrying an expected_turns budget whose visible turns stayed within 1.5× it. A budgeted task that failed counts as over budget.",
        blurb: (scoped) =>
            "% of budgeted tasks that stayed within 1.5× their expected turns (a budgeted task that failed counts as over budget) · runs with no budgeted task are omitted rather than plotted at 0" +
            (scoped ? " · scoped to the active filter" : ""),
        render: (props) => <TurnBudgetChart {...props} />,
    },
];

export function EfficiencyCharts({
    scoped = false,
    ...props
}: ChartProps & { scoped?: boolean }) {
    const [active, setActive] = useState<TabKey>(TABS[0].key);
    const tab = TABS.find((t) => t.key === active) ?? TABS[0];
    return (
        <div className="pt-4 mt-2 border-t border-dashed border-gray-200 space-y-1">
            <div className="flex items-start justify-between gap-3">
                <h2
                    className="text-sm font-semibold text-gray-900"
                    title={tab.title}
                >
                    {tab.heading}
                </h2>
                <span
                    className="inline-flex shrink-0 overflow-hidden rounded-md border border-gray-200 text-xs"
                    role="tablist"
                    aria-label="Efficiency metric"
                >
                    {TABS.map((t) => (
                        <button
                            key={t.key}
                            type="button"
                            role="tab"
                            aria-selected={t.key === active}
                            onClick={() => setActive(t.key)}
                            className={`px-2 py-0.5 transition-colors ${
                                t.key === active
                                    ? "bg-studio-blue text-white"
                                    : "bg-white text-gray-600 hover:bg-gray-50"
                            }`}
                        >
                            {t.label}
                        </button>
                    ))}
                </span>
            </div>
            <p className="text-xs text-gray-500">{tab.blurb(scoped)}</p>
            {tab.render(props)}
        </div>
    );
}
