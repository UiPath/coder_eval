import Link from "next/link";
import { fmtUtcDay, fmtUtcMinute } from "@/lib/format";
import { formatGap, type SeriesArm, type SeriesDef } from "@/lib/series";
import { loadSeriesIndexRow, type SeriesIndexRow } from "@/lib/series-data";
import { withSource } from "@/app/_lib/source-param";
import { SkelBar } from "@/app/_components/skeleton";
import { HealthPill } from "./health-pill";
import { Sparkline } from "./sparkline";

function armCount(n: number): string {
    return `${n} arm${n === 1 ? "" : "s"}`;
}

function pct(v: number | null | undefined): string {
    return v == null ? "—" : `${Math.round(v)}%`;
}

function SeriesLink({ def, arms }: { def: SeriesDef; arms: number | null }) {
    return (
        <>
            <Link
                href={`/series/${def.key}`}
                className="font-medium text-gray-900 hover:text-studio-blue"
            >
                {def.label}
            </Link>
            {arms != null && (
                <div className="text-xs text-gray-500">{armCount(arms)}</div>
            )}
        </>
    );
}

export function LatestCell({ row }: { row: SeriesIndexRow }) {
    const { arms, latest, gap } = row;
    if (arms.length === 1) return <span>{pct(latest[arms[0].id])}</span>;
    return (
        <span className="whitespace-nowrap">
            {arms.map((a: SeriesArm) => (
                <span key={a.id} className="mr-2">
                    <span className="text-gray-500">{a.label}</span>{" "}
                    {pct(latest[a.id])}
                </span>
            ))}
            {gap != null && (
                <span className="text-xs text-gray-500">
                    ({formatGap(gap, arms[1])})
                </span>
            )}
        </span>
    );
}

const CELL = "py-3 px-4 align-top";

export async function SeriesRow({ def }: { def: SeriesDef }) {
    let row: SeriesIndexRow;
    try {
        row = await loadSeriesIndexRow(def);
    } catch (err) {
        console.error(`[evalboard] series row ${def.key} failed:`, err);
        return (
            <tr className="border-b border-gray-100 last:border-b-0">
                <td className={CELL}>
                    <SeriesLink def={def} arms={null} />
                </td>
                <td colSpan={4} className={`${CELL} text-sm text-gray-500`}>
                    Could not load this series. Reload to retry.
                </td>
            </tr>
        );
    }
    const { lastRun } = row;
    return (
        <tr className="border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors">
            <td className={CELL}>
                <SeriesLink def={def} arms={row.arms.length} />
            </td>
            <td className={`${CELL} text-gray-700`}>{def.schedule || "—"}</td>
            <td className={CELL}>
                <div className="flex flex-wrap items-center gap-2">
                    {lastRun && (
                        <Link
                            href={withSource(`/runs/${lastRun.id}`, def.source.id)}
                            title={fmtUtcMinute(lastRun.startMs)}
                            className="font-mono text-xs tabular-nums text-gray-700 hover:text-studio-blue"
                        >
                            {fmtUtcDay(lastRun.startMs)}
                        </Link>
                    )}
                    <HealthPill health={row.health} />
                </div>
            </td>
            <td className={`${CELL} tabular-nums text-gray-900`}>
                <LatestCell row={row} />
            </td>
            <td className={CELL}>
                <Sparkline arms={row.arms} sparks={row.sparks} />
            </td>
        </tr>
    );
}

export function SeriesRowSkeleton({ def }: { def: SeriesDef }) {
    return (
        <tr className="border-b border-gray-100 last:border-b-0">
            <td className={CELL}>
                <SeriesLink def={def} arms={null} />
            </td>
            <td className={`${CELL} text-gray-700`}>{def.schedule || "—"}</td>
            {["w-24", "w-28", "w-24"].map((w, i) => (
                <td key={i} className={CELL}>
                    <div className="animate-pulse motion-reduce:animate-none">
                        <SkelBar className={`h-4 ${w}`} />
                    </div>
                </td>
            ))}
        </tr>
    );
}
