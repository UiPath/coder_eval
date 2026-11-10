import Link from "next/link";
import { notFound } from "next/navigation";
import { cache, Suspense } from "react";
import { mapWithConcurrency } from "@/lib/concurrency";
import {
    METRICS,
    armColor,
    gapRows,
    isSeriesKey,
    parseMetric,
    seriesByKey,
    trendRows,
    type SeriesDef,
    type SeriesMetric,
    type SeriesPoint,
} from "@/lib/series";
import { getAdhocRunListing } from "@/lib/overview";
import {
    DETAIL_RUNS,
    loadRunLevel,
    loadSeriesDetail,
    type RunLevel,
} from "@/lib/series-data";
import { SkelBar, SkelTable, SkelTile } from "@/app/_components/skeleton";
import { TrendChart, type ChartUnit } from "./trend-chart";
import { RunList } from "./run-list";
import { SourceRunTable } from "../_components/run-table";

export const dynamic = "force-dynamic";

const MAX_LIMIT = 300;
const ADHOC_LIMIT = 20;

const getDetail = cache((key: string, limit: number) =>
    loadSeriesDetail(seriesByKey(key), limit),
);

const LEVEL_FIELD: Record<Exclude<SeriesMetric, "pass">, keyof RunLevel> = {
    cost: "costPerTask",
    errors: "errorShare",
    wall: "timePerPassedTask",
};

const UNIT: Record<SeriesMetric, ChartUnit> = {
    pass: "percent",
    cost: "usd",
    errors: "share",
    wall: "seconds",
};

function parseLimit(raw: string | string[] | undefined): number {
    const v = Array.isArray(raw) ? raw[0] : raw;
    const n = v ? Number.parseInt(v, 10) : NaN;
    if (!Number.isFinite(n) || n < 1) return DETAIL_RUNS;
    return Math.min(n, MAX_LIMIT);
}

function href(key: string, metric: SeriesMetric, limit: number): string {
    const sp = new URLSearchParams();
    if (metric !== "pass") sp.set("m", metric);
    if (limit !== DETAIL_RUNS) sp.set("limit", String(limit));
    const qs = sp.toString();
    return qs ? `/series/${key}?${qs}` : `/series/${key}`;
}

function Empty({ children }: { children: string }) {
    return <p className="text-sm text-gray-500 py-8 text-center">{children}</p>;
}

async function TrendSection({
    def,
    metric,
    limit,
}: {
    def: SeriesDef;
    metric: SeriesMetric;
    limit: number;
}) {
    const detail = await getDetail(def.key, limit);
    let points: SeriesPoint[] = detail.points;
    let arms = detail.arms;
    if (metric !== "pass") {
        const field = LEVEL_FIELD[metric];
        const levels = await mapWithConcurrency(points, 8, (p) =>
            loadRunLevel(def, p.id),
        );
        arms = [{ id: "run", label: METRICS.find((m) => m.key === metric)!.label }];
        points = points.flatMap((p, i) => {
            const v = levels[i]?.[field];
            return v == null ? [] : [{ ...p, values: { run: v } }];
        });
    }
    const { rows, lines } = trendRows(points, arms);
    const gap = metric === "pass" ? gapRows(points, arms) : [];
    return (
        <>
            <section className="border border-gray-200 rounded-lg bg-white p-4 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <h2 className="text-sm font-semibold text-gray-900">Trend</h2>
                    <nav aria-label="Metric" className="flex flex-wrap gap-1 text-xs">
                        {METRICS.map((m) => (
                            <Link
                                key={m.key}
                                href={href(def.key, m.key, limit)}
                                scroll={false}
                                title={m.title}
                                aria-current={m.key === metric ? "page" : undefined}
                                className={`rounded-full border px-3 py-1 ${
                                    m.key === metric
                                        ? "border-studio-blue bg-blue-50 text-studio-blue"
                                        : "border-gray-200 text-gray-600 hover:bg-gray-50"
                                }`}
                            >
                                {m.label}
                            </Link>
                        ))}
                    </nav>
                </div>
                {rows.length > 0 ? (
                    <TrendChart rows={rows} lines={lines} unit={UNIT[metric]} />
                ) : (
                    <Empty>Nothing to plot yet.</Empty>
                )}
            </section>
            {arms.length === 2 && metric === "pass" && gap.length > 0 && (
                <section className="border border-gray-200 rounded-lg bg-white p-4 space-y-3">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Gap between arms{" "}
                        <span className="font-normal text-gray-500">
                            ({arms[1].label} minus {arms[0].label}, points)
                        </span>
                    </h2>
                    <TrendChart
                        rows={gap}
                        lines={[
                            {
                                key: "gap",
                                label: `${arms[1].label} minus ${arms[0].label}`,
                                color: armColor(1, 2),
                            },
                        ]}
                        unit="points"
                    />
                </section>
            )}
        </>
    );
}

async function RunsSection({
    def,
    metric,
    limit,
}: {
    def: SeriesDef;
    metric: SeriesMetric;
    limit: number;
}) {
    const detail = await getDetail(def.key, limit);
    const more = detail.total > detail.runs.length && limit < MAX_LIMIT;
    return (
        <RunList
            def={def}
            arms={detail.arms}
            runs={detail.runs}
            total={detail.total}
            moreHref={
                more
                    ? href(def.key, metric, Math.min(limit + DETAIL_RUNS, MAX_LIMIT))
                    : null
            }
        />
    );
}

// A canonical series owns its whole container, so the container's ad-hoc runs
// are reachable only from here.
async function AdhocSection({ def }: { def: SeriesDef }) {
    const adhoc = await getAdhocRunListing(ADHOC_LIMIT, def.source);
    if (adhoc.total === 0) return null;
    return (
        <SourceRunTable
            rows={adhoc.rows}
            totalCandidates={adhoc.total}
            showMoreHref={null}
            sourceId={def.source.id}
            heading="Ad-hoc runs"
            note="Queued with adhoc=true · left off the trend and the run list above"
        />
    );
}

export default async function SeriesDetailPage({
    params,
    searchParams,
}: {
    params: Promise<{ key: string }>;
    searchParams: Promise<{ m?: string | string[]; limit?: string | string[] }>;
}) {
    const { key } = await params;
    if (!isSeriesKey(key)) notFound();
    const sp = await searchParams;
    const metric = parseMetric(sp.m);
    const limit = parseLimit(sp.limit);
    const def = seriesByKey(key);
    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <Link
                    href="/series"
                    className="text-xs text-studio-blue hover:underline"
                >
                    All scheduled
                </Link>
                <h1 className="text-xl font-semibold text-gray-900">
                    {def.label}
                </h1>
                {def.description && (
                    <p className="text-sm text-gray-500 max-w-3xl">
                        {def.description}
                    </p>
                )}
            </div>
            <Suspense
                key={`${metric}:${limit}`}
                fallback={
                    <SkelTile className="h-72">
                        <SkelBar className="h-full w-full" />
                    </SkelTile>
                }
            >
                <TrendSection def={def} metric={metric} limit={limit} />
            </Suspense>
            <Suspense key={limit} fallback={<SkelTable rows={6} />}>
                <RunsSection def={def} metric={metric} limit={limit} />
            </Suspense>
            {def.membership === "canonical" && (
                <Suspense fallback={<SkelTable rows={4} />}>
                    <AdhocSection def={def} />
                </Suspense>
            )}
        </div>
    );
}
