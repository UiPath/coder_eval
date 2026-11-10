// A series is a recurring pipeline whose runs are points on one trend. A run
// joins through `series` in its meta.json, or, for a source whose canonical runs
// all belong to one series, through its id alone. Client-safe: the blob reads
// live in lib/series-data.ts.

import {
    SCRIBE_SOURCE,
    SKILLS_SOURCE,
    type Source,
} from "./sources";

export interface SeriesArm {
    id: string;
    label: string;
}

// single: one line from the run overview. variants: arms from experiment.json,
// in the declared order. auto: whatever the run carries.
export type ArmsSpec =
    | { kind: "single" }
    | { kind: "variants"; arms: readonly SeriesArm[] }
    | { kind: "auto" };

export type Membership = "meta" | "canonical";

export interface SeriesDef {
    key: string;
    label: string;
    description: string;
    arms: ArmsSpec;
    source: Source;
    membership: Membership;
}

const V1_V2: ArmsSpec = {
    kind: "variants",
    arms: [
        { id: "v1", label: "v1" },
        { id: "v2", label: "v2" },
    ],
};

export const SERIES: readonly SeriesDef[] = [
    {
        key: "maestro-v1-v2-codex",
        label: "Maestro v1 vs v2 · codex",
        description:
            "Maestro case and BPMN tasks on the same corpus and tenant, run once with two skill sets on the Codex harness. The only difference between the arms is which skills the agent loads.",
        arms: V1_V2,
        source: SKILLS_SOURCE,
        membership: "meta",
    },
    {
        key: "maestro-v1-v2-delegate",
        label: "Maestro v1 vs v2 · delegate",
        description:
            "Maestro case and BPMN tasks on the same corpus and tenant, run once with two skill sets on the Delegate harness. The only difference between the arms is which skills the agent loads.",
        arms: V1_V2,
        source: SKILLS_SOURCE,
        membership: "meta",
    },
    {
        key: "n8n-vs-flow",
        label: "n8n vs Flow",
        description:
            "The same workflow-building scenarios built on n8n and on Flow, run as one experiment with two arms.",
        arms: {
            kind: "variants",
            arms: [
                { id: "n8n", label: "n8n" },
                { id: "flow-v2", label: "Flow" },
            ],
        },
        source: SKILLS_SOURCE,
        membership: "meta",
    },
    {
        key: "ete-weekly",
        label: "ETE weekly",
        description:
            "End-to-end evaluation of the skills against Automation Suite.",
        arms: { kind: "single" },
        source: SKILLS_SOURCE,
        membership: "meta",
    },
    {
        key: "scribe",
        label: "Scribe",
        description:
            "The Autopilot (aria/Composer) suite, run by the UiPath.Autopilot.Eval.Manual pipeline and uploaded to the aria-runs container.",
        arms: { kind: "single" },
        source: SCRIBE_SOURCE,
        membership: "canonical",
    },
];

export const SERIES_KEY_PATTERN = /^[\w.-]{1,64}$/;

export function isSeriesKey(key: unknown): key is string {
    return typeof key === "string" && SERIES_KEY_PATTERN.test(key);
}

export function knownSeries(key: string): SeriesDef | undefined {
    return SERIES.find((s) => s.key === key);
}

export function unknownSeries(key: string): SeriesDef {
    return {
        key,
        label: key,
        description: "",
        arms: { kind: "auto" },
        source: SKILLS_SOURCE,
        membership: "meta",
    };
}

export function seriesByKey(key: string): SeriesDef {
    return knownSeries(key) ?? unknownSeries(key);
}

// The table in its own order, then keys found only in data, sorted.
export function listSeries(extraKeys: readonly string[] = []): SeriesDef[] {
    return [
        ...SERIES,
        ...[...new Set(extraKeys)]
            .filter((k) => !knownSeries(k))
            .sort()
            .map(unknownSeries),
    ];
}

// ---------- membership ----------

export interface SeriesRunRef {
    id: string;
    startMs: number;
    dateShaped: boolean;
    adhoc: boolean;
    title: string | null;
    series: string | null;
    excluded: string | null;
}

type MembershipFields = Pick<SeriesRunRef, "series" | "dateShaped" | "adhoc">;

export function isSeriesMember(def: SeriesDef, run: MembershipFields): boolean {
    return def.membership === "meta"
        ? run.series === def.key
        : run.dateShaped && !run.adhoc;
}

export function seriesForRun(
    sourceId: string,
    run: MembershipFields,
): SeriesDef | null {
    const candidates = run.series ? [seriesByKey(run.series)] : SERIES;
    return (
        candidates.find((d) => d.source.id === sourceId && isSeriesMember(d, run)) ??
        null
    );
}

export function membersOf(
    def: SeriesDef,
    runs: readonly SeriesRunRef[],
): SeriesRunRef[] {
    return runs
        .filter((r) => isSeriesMember(def, r))
        .sort((a, b) => b.startMs - a.startMs || b.id.localeCompare(a.id));
}

export function plotted(runs: readonly SeriesRunRef[]): SeriesRunRef[] {
    return runs.filter((r) => !r.excluded);
}

export function unlistedSeriesKeys(runs: readonly SeriesRunRef[]): string[] {
    const keys = new Set<string>();
    for (const r of runs) {
        if (r.series && !knownSeries(r.series)) keys.add(r.series);
    }
    return [...keys].sort();
}

// ---------- excluded reasons ----------

const RUN_ID_IN_TEXT =
    /(?<![\w.-])(?:[A-Za-z][\w.-]*?-)?\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?![\w.-])/g;

export type ReasonPart = { text: string } | { runId: string };

export function splitReason(reason: string): ReasonPart[] {
    const parts: ReasonPart[] = [];
    let last = 0;
    for (const m of reason.matchAll(RUN_ID_IN_TEXT)) {
        if (m.index > last) parts.push({ text: reason.slice(last, m.index) });
        parts.push({ runId: m[0] });
        last = m.index + m[0].length;
    }
    if (last < reason.length) parts.push({ text: reason.slice(last) });
    return parts;
}

// ---------- arms and values ----------

export const SINGLE_ARM: SeriesArm = { id: "all", label: "Pass rate" };

// A declared arm no run carries is dropped once any run has data; an id the
// table does not declare is appended under its own id.
export function resolveArms(
    spec: ArmsSpec,
    observed: readonly string[],
): SeriesArm[] {
    if (spec.kind === "single") return [SINGLE_ARM];
    const seen = [...new Set(observed)];
    const declared = spec.kind === "variants" ? spec.arms : [];
    if (seen.length === 0) {
        return declared.length > 0 ? [...declared] : [SINGLE_ARM];
    }
    const known = declared.filter((a) => seen.includes(a.id));
    const extra = seen
        .filter((id) => !declared.some((a) => a.id === id))
        .sort()
        .map((id) => (id === SINGLE_ARM.id ? SINGLE_ARM : { id, label: id }));
    return [...known, ...extra];
}

export type ArmValues = Record<string, number | null>;

export interface SeriesPoint {
    id: string;
    startMs: number;
    values: ArmValues;
}

export function latestValues(
    points: readonly SeriesPoint[],
    arms: readonly SeriesArm[],
): ArmValues {
    const out: ArmValues = {};
    for (const a of arms) out[a.id] = points[0]?.values[a.id] ?? null;
    return out;
}

export function armGap(
    values: ArmValues,
    arms: readonly SeriesArm[],
): number | null {
    if (arms.length !== 2) return null;
    const a = values[arms[0].id];
    const b = values[arms[1].id];
    return a == null || b == null ? null : b - a;
}

// ---------- charts ----------

export const ARM_COLORS = ["#6b7280", "#1976D2", "#d97706", "#15803d", "#7c3aed"];

export function armColor(index: number, count: number): string {
    return count === 1 ? ARM_COLORS[1] : ARM_COLORS[index % ARM_COLORS.length];
}

export interface TrendLine {
    key: string;
    label: string;
    color: string;
}

export type TrendRow = { timestamp: number } & Record<string, number>;

// Keys are positional: recharts reads a "." in a data key as a nested path.
export function trendRows(
    points: readonly SeriesPoint[],
    arms: readonly SeriesArm[],
): { rows: TrendRow[]; lines: TrendLine[] } {
    const lines = arms.map((a, i) => ({
        key: `a${i}`,
        label: a.label,
        color: armColor(i, arms.length),
    }));
    const rows: TrendRow[] = [];
    for (const p of [...points].sort((a, b) => a.startMs - b.startMs)) {
        const row: TrendRow = { timestamp: p.startMs };
        let any = false;
        arms.forEach((a, i) => {
            const v = p.values[a.id];
            if (v != null) {
                row[lines[i].key] = v;
                any = true;
            }
        });
        if (any) rows.push(row);
    }
    return { rows, lines };
}

export function gapRows(
    points: readonly SeriesPoint[],
    arms: readonly SeriesArm[],
): TrendRow[] {
    const rows: TrendRow[] = [];
    for (const p of [...points].sort((a, b) => a.startMs - b.startMs)) {
        const gap = armGap(p.values, arms);
        if (gap != null) rows.push({ timestamp: p.startMs, gap });
    }
    return rows;
}

export type SeriesMetric = "pass" | "cost" | "errors" | "wall";

export const METRICS: readonly {
    key: SeriesMetric;
    label: string;
    title: string;
}[] = [
    { key: "pass", label: "Pass rate", title: "Passed ÷ graded tasks, per arm" },
    {
        key: "cost",
        label: "Cost per task",
        title: "Run total cost ÷ task rows that ran, over every arm",
    },
    {
        key: "errors",
        label: "Error share",
        title: "Tasks that ended in ERROR or BUILD_FAILED ÷ graded tasks, over every arm",
    },
    {
        key: "wall",
        label: "Wall-clock",
        title: "Agent seconds of every task that ran ÷ tasks passed, over every arm",
    },
];

export function parseMetric(raw: string | string[] | undefined): SeriesMetric {
    const v = Array.isArray(raw) ? raw[0] : raw;
    return METRICS.find((m) => m.key === v)?.key ?? "pass";
}

// ---------- sparkline ----------

export function sparkDomain(
    values: readonly number[],
    minSpan = 10,
): [number, number] {
    if (values.length === 0) return [0, 100];
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (hi - lo < minSpan) {
        const mid = (hi + lo) / 2;
        lo = mid - minSpan / 2;
        hi = mid + minSpan / 2;
    }
    if (lo < 0) [lo, hi] = [0, Math.min(100, hi - lo)];
    if (hi > 100) [lo, hi] = [Math.max(0, lo - (hi - 100)), 100];
    return [lo, hi];
}

export interface SparkLine {
    d: string;
    last: { x: number; y: number } | null;
}

export function sparkLine(
    values: readonly (number | null)[],
    width: number,
    height: number,
    domain: [number, number],
    pad = 2,
): SparkLine {
    const [lo, hi] = domain;
    const span = hi - lo || 1;
    const step = values.length > 1 ? (width - 2 * pad) / (values.length - 1) : 0;
    const segments: string[] = [];
    let pen = false;
    let last: SparkLine["last"] = null;
    values.forEach((v, i) => {
        if (v == null) {
            pen = false;
            return;
        }
        const x = pad + i * step;
        const y = pad + (1 - (v - lo) / span) * (height - 2 * pad);
        segments.push(`${pen ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`);
        pen = true;
        last = { x, y };
    });
    return { d: segments.join(" "), last };
}
