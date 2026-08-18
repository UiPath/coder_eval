// Presentational server component. Renders the Needs-Attention hero + five
// panels in the evalboard house style. Detailed score numbers ride on native
// `title` tooltips (hover); each list is expandable via native <details> so
// offenders tied at the same level aren't silently truncated — all CSS, no
// client JS.
import type { ReactNode } from "react";
import Link from "next/link";
import { fmtTaskSeconds } from "@/lib/timing";
import { humanizeTaskId } from "@/lib/format";
import { passBarClass, passClassRatio } from "@/lib/pass-rate";
import { HarnessSelector } from "@/app/_components/harness-selector";
import { harnessShortLabel } from "@/app/_components/harness-badge";
import {
    FAIL_WEIGHT,
    REG_WEIGHT,
    TIME_WEIGHT,
    type AttentionRow,
    type WatchlistData,
} from "@/lib/watchlist";

const pct = (n: number) => `${Math.round(n * 100)}%`;

function skillHref(skill: string) {
    return `/?tag=${encodeURIComponent(skill)}`;
}
function taskHref(runId: string, taskId: string) {
    return `/runs/${runId}/${taskId}`;
}

function Empty({ children }: { children: ReactNode }) {
    return <p className="text-sm text-gray-400 py-2">{children} 🎉</p>;
}

// Rows shown before the "Show all" expander kicks in, per list.
const CAP = {
    hero: 5,
    neverPassed: 8,
    leaderboard: 10,
    streaks: 8,
    volatility: 6,
    timeOverage: 8,
} as const;

// Renders the first `cap` rows, then — if there are more — a native
// <details> expander that reveals the rest (and collapses back). When a
// `sameLevel` predicate is given, the summary calls out how many hidden rows
// are tied with the last visible one, since equal-severity offenders are the
// whole reason an exec would expand. CSS-only (no client JS).
function ExpandableList<T>({
    items,
    cap,
    render,
    sameLevel,
}: {
    items: T[];
    cap: number;
    render: (item: T, index: number) => ReactNode;
    sameLevel?: (a: T, b: T) => boolean;
}) {
    if (items.length <= cap) return <>{items.map(render)}</>;
    const head = items.slice(0, cap);
    const rest = items.slice(cap);
    const last = head[head.length - 1];
    const tied = sameLevel ? rest.filter((r) => sameLevel(r, last)).length : 0;
    return (
        <>
            {head.map(render)}
            {/* flex-col + order-1 pushes the summary below the revealed rows
                when open, so "Show fewer" sits at the bottom of the expanded
                list instead of mid-list (summary must stay first in the DOM). */}
            <details className="mt-0.5 flex flex-col">
                <summary className="order-1 cursor-pointer list-none select-none py-1 text-[11px] font-medium text-studio-blue hover:underline">
                    <span className="[[open]_&]:hidden">
                        ▸ Show all {items.length}
                        {tied > 0 ? ` (${tied} more tied)` : ""}
                    </span>
                    <span className="hidden [[open]_&]:inline">▾ Show fewer</span>
                </summary>
                {rest.map((item, i) => render(item, i + cap))}
            </details>
        </>
    );
}

function Panel({
    title,
    sub,
    info,
    children,
}: {
    title: string;
    sub: string;
    /** Optional precise definition, shown as a native hover tooltip on an ⓘ. */
    info?: string;
    children: ReactNode;
}) {
    return (
        <section className="bg-white border border-gray-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-1">
                {title}
                {info ? (
                    <span
                        className="cursor-help select-none text-[11px] font-normal text-gray-300 hover:text-gray-500"
                        title={info}
                        aria-label={info}
                    >
                        ⓘ
                    </span>
                ) : null}
            </h3>
            <p className="text-[10px] uppercase tracking-wide text-gray-400 mt-0.5 mb-3">
                {sub}
            </p>
            {children}
        </section>
    );
}

// Inline SVG for the volatility panel: a line tracing each run's pass rate
// (oldest → newest) over a faint band marking mean ± std-dev (the "swing").
// Unlike the old binary bars — green-tall ≥50%, red-short <50% — the line's
// height tracks the *actual* pass rate, so a skill that bounces 100%↔50% reads
// as a jagged line over a tall band (flaky) instead of a row of identical
// green bars. Pure SVG, no client JS.
function VarianceSparkline({ values, std }: { values: number[]; std: number }) {
    const W = 96;
    const H = 20;
    const pad = 2; // keep the trace and dots off the top & bottom edges
    const n = values.length;
    const y = (p: number) => pad + (1 - p) * (H - 2 * pad);
    const x = (i: number) => (n <= 1 ? W / 2 : (i / (n - 1)) * W);
    const m = values.reduce((a, b) => a + b, 0) / n;
    const bandTop = y(Math.min(1, m + std));
    const bandBot = y(Math.max(0, m - std));
    const trace = values.map((p, i) => `${x(i)},${y(p)}`).join(" ");
    return (
        <svg
            width={W}
            height={H}
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            role="img"
        >
            <title>{values.map((p) => pct(p)).join(" → ")}</title>
            {/* mean ± swing band — a taller band means a flakier skill */}
            <rect
                x={0}
                y={bandTop}
                width={W}
                height={Math.max(1, bandBot - bandTop)}
                className="fill-amber-200"
                fillOpacity={0.55}
            />
            {/* mean line */}
            <line
                x1={0}
                y1={y(m)}
                x2={W}
                y2={y(m)}
                className="stroke-amber-300"
                strokeWidth={0.5}
            />
            {/* pass-rate trace */}
            <polyline
                points={trace}
                fill="none"
                className="stroke-green-600"
                strokeWidth={1.25}
                strokeLinejoin="round"
                strokeLinecap="round"
            />
            {values.map((p, i) => (
                <circle
                    key={i}
                    cx={x(i)}
                    cy={y(p)}
                    r={1.4}
                    className="fill-green-700"
                />
            ))}
        </svg>
    );
}

// Per-component cell colors: dominant component is emphasized in its segment
// color, non-zero others are muted, zeros are near-invisible.
const SEG_TEXT = ["text-red-700", "text-studio-blue", "text-amber-700"] as const;

function HeroRow({
    row,
    rank,
    max,
}: {
    row: AttentionRow;
    rank: number;
    max: number;
}) {
    const w = (pts: number) => `${max > 0 ? (pts / max) * 100 : 0}%`;
    // Same argmax as attentionReason — which component drove the score.
    const segs = [row.segFail, row.segReg, row.segTime];
    const top = segs.indexOf(Math.max(...segs));
    const cell = (i: number, zero: boolean) =>
        top === i
            ? `font-semibold ${SEG_TEXT[i]}`
            : zero
              ? "text-gray-300"
              : "text-gray-500";
    // Round for display first so the labels never contradict themselves
    // (e.g. a zero-point trend arrow or "1.0×" shown as over budget).
    const regPts = Math.round(row.regression * 100);
    const timesX = (1 + Math.min(row.timeOverage, 1)).toFixed(1);
    const overBudget = timesX !== "1.0";
    const tip =
        `${row.reason}\n` +
        `pass ${pct(row.passRate)} · recent ${pct(row.recentPassRate)} vs prior ${pct(row.prevPassRate)}\n` +
        `score ${Math.round(row.score)}/100 = ${row.segFail.toFixed(1)} fail (${FAIL_WEIGHT}·${row.failRate.toFixed(2)}) + ${row.segReg.toFixed(1)} reg (${REG_WEIGHT}·${row.regression.toFixed(2)}) + ${row.segTime.toFixed(1)} time (${TIME_WEIGHT}·${row.timeOverage.toFixed(2)})`;
    return (
        <div
            className="flex items-center gap-3.5 py-2.5 border-b border-gray-100 last:border-b-0"
            title={tip}
        >
            <span
                className={`w-5 text-center font-extrabold text-base ${rank === 1 ? "text-red-700" : "text-gray-400"}`}
            >
                {rank}
            </span>
            <span className="min-w-[190px] flex flex-col">
                <Link
                    href={skillHref(row.skill)}
                    className="self-start font-mono text-xs font-semibold text-gray-900 hover:text-studio-blue"
                >
                    {row.skill}
                </Link>
                <span
                    className="text-[10px] text-gray-400"
                    title="Distinct tasks behind this row — small n is a noisy signal"
                >
                    {row.tasks} task{row.tasks === 1 ? "" : "s"}
                </span>
            </span>
            <div className="flex-1 min-w-[180px] h-[18px] rounded-[9px] bg-gray-100 overflow-hidden flex">
                <span className="h-full bg-red-500" style={{ width: w(row.segFail) }} />
                <span className="h-full bg-studio-blue" style={{ width: w(row.segReg) }} />
                <span className="h-full bg-amber-400" style={{ width: w(row.segTime) }} />
            </div>
            <span className="text-[11.5px] tabular-nums flex">
                <span className={`w-[64px] ${cell(0, row.failRate === 0)}`}>
                    {pct(row.failRate)}
                </span>
                <span className={`w-[100px] ${cell(1, regPts === 0)}`}>
                    {regPts > 0
                        ? `${pct(row.prevPassRate)} → ${pct(row.recentPassRate)}`
                        : "steady"}
                </span>
                <span className={`w-[80px] ${cell(2, !overBudget)}`}>
                    {overBudget
                        ? `${row.timeOverage >= 1 ? "≥" : ""}${timesX}×`
                        : "on budget"}
                </span>
            </span>
        </div>
    );
}

// Column headers aligned with HeroRow's layout (rank + skill + bar are left
// blank so the labels land over the three breakdown cells).
function HeroHeader() {
    return (
        <div className="flex items-center gap-3.5 pb-1">
            <span className="w-5" />
            <span className="min-w-[190px]" />
            <span className="flex-1 min-w-[180px]" />
            <span className="flex text-[10px] uppercase tracking-wide text-gray-400">
                <span className="w-[64px]">fail rate</span>
                <span className="w-[100px]">pass trend</span>
                <span className="w-[80px]">expected time</span>
            </span>
        </div>
    );
}

export function WatchlistView({
    data,
    activeHarness,
    harnesses,
}: {
    data: WatchlistData;
    activeHarness: string;
    harnesses: readonly string[];
}) {
    // Bars are scaled to the worst offender (full track) so score differences
    // within the list are legible — not to the theoretical max of 100, which
    // renders every realistic score as a near-identical sliver.
    const max = Math.max(1, ...data.topAttention.map((r) => r.score));
    return (
        <div>
            <div className="flex items-center gap-3 flex-wrap">
                <h1 className="text-[22px] font-bold text-gray-900 border-l-[3px] border-uipath-orange pl-2.5">
                    Watchlist
                </h1>
                <div className="ml-auto flex items-center gap-3">
                    <HarnessSelector
                        current={activeHarness}
                        harnesses={harnesses}
                    />
                    <span className="text-[11px] text-gray-600 bg-gray-100 px-3 py-1 rounded-full font-semibold whitespace-nowrap">
                        last {data.windowSize}{" "}
                        {harnessShortLabel(activeHarness)} runs
                    </span>
                </div>
            </div>
            <p className="text-gray-500 text-[13px] mt-1.5 mb-6">
                What leadership should be watching — ranked by where the signal
                says to look first, for the selected harness.
            </p>

            {/* HERO */}
            <section className="bg-white border border-gray-200 rounded-lg p-5 mb-6">
                <div className="flex items-center gap-2">
                    <h2 className="text-base font-bold text-gray-900">
                        Needs Attention
                    </h2>
                    <span className="text-[10px] font-bold uppercase tracking-wide text-uipath-orange bg-[#fff3ee] border border-[#ffd9cc] px-2 py-0.5 rounded-full">
                        Top 5
                    </span>
                </div>
                <p className="text-gray-500 text-[11px] mt-1 mb-4">
                    Ranked by {FAIL_WEIGHT}·fail-rate + {REG_WEIGHT}·regression
                    + {TIME_WEIGHT}·time-overage · bar = severity, scaled to
                    the top row · segments = what drove it
                </p>
                {data.topAttention.length === 0 ? (
                    <Empty>Nothing needs attention</Empty>
                ) : (
                    <>
                        <HeroHeader />
                        <ExpandableList
                            items={data.topAttention}
                            cap={CAP.hero}
                            sameLevel={(a, b) =>
                                Math.round(a.score) === Math.round(b.score)
                            }
                            render={(row, i) => (
                                <HeroRow
                                    key={row.skill}
                                    row={row}
                                    rank={i + 1}
                                    max={max}
                                />
                            )}
                        />
                    </>
                )}
                <div className="mt-3.5 flex gap-4 text-[11px] text-gray-600">
                    <span>
                        <i className="inline-block w-2.5 h-2.5 rounded-sm bg-red-500 mr-1.5 align-middle" />
                        fail-rate
                    </span>
                    <span>
                        <i className="inline-block w-2.5 h-2.5 rounded-sm bg-studio-blue mr-1.5 align-middle" />
                        regression
                    </span>
                    <span>
                        <i className="inline-block w-2.5 h-2.5 rounded-sm bg-amber-400 mr-1.5 align-middle" />
                        time-overage
                    </span>
                    <span className="ml-auto text-gray-400">
                        hover a row for the score arithmetic
                    </span>
                </div>
            </section>

            {/* PANELS */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <Panel
                    title="🔴 Never passed"
                    sub="Chronically broken · failed every run in window"
                >
                    {data.neverPassed.length === 0 ? (
                        <Empty>None</Empty>
                    ) : (
                        <ExpandableList
                            items={data.neverPassed}
                            cap={CAP.neverPassed}
                            sameLevel={(a, b) => a.appeared === b.appeared}
                            render={(r) => (
                                <div
                                    key={r.taskId}
                                    className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs"
                                >
                                    <Link
                                        href={taskHref(r.latestRunId, r.taskId)}
                                        className="font-mono text-[11px] text-gray-700 hover:text-studio-blue"
                                    >
                                        {humanizeTaskId(r.taskId)}
                                    </Link>
                                    {r.skill && (
                                        <span className="bg-indigo-50 text-indigo-700 rounded-full px-2 py-0.5 text-[10px] font-semibold">
                                            {r.skill}
                                        </span>
                                    )}
                                    <span className="ml-auto font-bold text-red-700">
                                        0/{r.appeared}
                                    </span>
                                </div>
                            )}
                        />
                    )}
                </Panel>

                <Panel title="📉 Skills leaderboard" sub="Pass rate · worst → best">
                    {data.leaderboard.length === 0 ? (
                        <Empty>No skills</Empty>
                    ) : (
                        <ExpandableList
                            items={data.leaderboard}
                            cap={CAP.leaderboard}
                            sameLevel={(a, b) => pct(a.passRate) === pct(b.passRate)}
                            render={(r) => (
                                <div
                                    key={r.skill}
                                    className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs"
                                >
                                    <Link
                                        href={skillHref(r.skill)}
                                        className="font-mono text-[11px] text-gray-700 min-w-[120px] hover:text-studio-blue"
                                    >
                                        {r.skill}
                                    </Link>
                                    <span className="flex-1 min-w-[70px] h-[7px] rounded bg-gray-100 overflow-hidden">
                                        <i
                                            className={`block h-full ${passBarClass(r.passRate * 100)}`}
                                            style={{ width: pct(r.passRate) }}
                                        />
                                    </span>
                                    <span
                                        className={`ml-auto font-bold ${passClassRatio(r.passRate)}`}
                                    >
                                        {pct(r.passRate)}
                                    </span>
                                </div>
                            )}
                        />
                    )}
                </Panel>

                <Panel
                    title="🧯 Failed in sequence, never fixed"
                    sub="Active losing streak from the latest run"
                >
                    {data.streaks.length === 0 ? (
                        <Empty>No active streaks</Empty>
                    ) : (
                        <ExpandableList
                            items={data.streaks}
                            cap={CAP.streaks}
                            sameLevel={(a, b) => a.streak === b.streak}
                            render={(r) => (
                                <div
                                    key={r.taskId}
                                    className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs"
                                >
                                    <Link
                                        href={taskHref(r.latestRunId, r.taskId)}
                                        className="font-mono text-[11px] text-gray-700 hover:text-studio-blue"
                                    >
                                        {humanizeTaskId(r.taskId)}
                                    </Link>
                                    <span
                                        className={`ml-auto font-semibold rounded-full px-2.5 py-0.5 text-[11px] border ${r.streak >= 5 ? "bg-red-50 text-red-700 border-red-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}
                                    >
                                        {r.streak} in a row
                                    </span>
                                </div>
                            )}
                        />
                    )}
                </Panel>

                <Panel
                    title="🎢 Yee-Yaw — least stable"
                    sub="How much a skill's pass rate swings run-to-run"
                    info="Standard deviation of the skill's per-run pass rate across the window. Higher = less consistent run-to-run (flakier). A run's pass rate = passing tasks ÷ total tasks for that skill in that run."
                >
                    {data.volatility.length === 0 ? (
                        <Empty>All stable</Empty>
                    ) : (
                        <ExpandableList
                            items={data.volatility}
                            cap={CAP.volatility}
                            sameLevel={(a, b) =>
                                Math.round(a.volatility * 100) ===
                                Math.round(b.volatility * 100)
                            }
                            render={(r) => (
                                <div
                                    key={r.skill}
                                    className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs"
                                >
                                    <Link
                                        href={skillHref(r.skill)}
                                        className="font-mono text-[11px] text-gray-700 min-w-[105px] hover:text-studio-blue"
                                    >
                                        {r.skill}
                                    </Link>
                                    <VarianceSparkline
                                        values={[...r.sparkline].reverse()}
                                        std={r.volatility}
                                    />
                                    <span className="ml-auto font-bold text-amber-700">
                                        ±{Math.round(r.volatility * 100)}%
                                    </span>
                                </div>
                            )}
                        />
                    )}
                </Panel>

                <Panel
                    title="🐌 Slow-task offenders"
                    sub="Passing, but well past their expected time"
                >
                    {data.timeOverage.length === 0 ? (
                        <Empty>All within expected time</Empty>
                    ) : (
                        <ExpandableList
                            items={data.timeOverage}
                            cap={CAP.timeOverage}
                            sameLevel={(a, b) =>
                                a.avgTimeRatio.toFixed(1) === b.avgTimeRatio.toFixed(1)
                            }
                            render={(r) => (
                                <div
                                    key={r.skill}
                                    className="flex items-center gap-2 py-1.5 border-b border-gray-100 last:border-b-0 text-xs"
                                >
                                    <span className="font-mono text-[11px] text-gray-700 min-w-[105px]">
                                        {r.skill}
                                    </span>
                                    <span className="flex-1 text-[11px] text-gray-500">
                                        {fmtTaskSeconds(r.avgSeconds)} /{" "}
                                        {fmtTaskSeconds(r.avgExpectedSeconds)}{" "}
                                        expected
                                    </span>
                                    <span
                                        className={`ml-auto font-semibold rounded-full px-2.5 py-0.5 text-[11px] border ${r.avgTimeRatio > 1.5 ? "bg-red-50 text-red-700 border-red-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}
                                    >
                                        {r.avgTimeRatio.toFixed(1)}×
                                    </span>
                                </div>
                            )}
                        />
                    )}
                </Panel>
            </div>

            <p className="text-gray-400 text-[11px] mt-5">
                Task rows link to their task detail · skill rows filter the
                dashboard to that skill.
            </p>
        </div>
    );
}
