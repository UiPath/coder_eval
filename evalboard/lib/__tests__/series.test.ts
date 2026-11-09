import { describe, expect, test } from "vitest";
import {
    GROUP_ORDER,
    SERIES,
    SINGLE_ARM,
    armGap,
    formatGap,
    gapRows,
    groupSeries,
    healthLabel,
    isSeriesKey,
    isSeriesMember,
    latestValues,
    membersOf,
    parseMetric,
    plotted,
    reasonRunIds,
    resolveArms,
    seriesByKey,
    seriesHealth,
    sparkDomain,
    sparkLine,
    splitReason,
    trendRows,
    unlistedSeriesKeys,
    type SeriesPoint,
    type SeriesRunRef,
} from "../series";
import { SCRIBE_SOURCE, SKILLS_SOURCE } from "../sources";

function ref(over: Partial<SeriesRunRef> & { id: string }): SeriesRunRef {
    return {
        startMs: Date.UTC(2026, 10, 9, 16),
        dateShaped: false,
        adhoc: true,
        title: null,
        series: null,
        excluded: null,
        ...over,
    };
}

function onDay(day: number, over: Partial<SeriesRunRef> = {}): SeriesRunRef {
    return ref({
        id: `adhoc-2026-11-${String(day).padStart(2, "0")}_16-00-00`,
        startMs: Date.UTC(2026, 10, day, 16),
        series: "maestro-v1-v2-codex",
        ...over,
    });
}

describe("series table", () => {
    test("lists the five series with unique keys, in a known group", () => {
        expect(SERIES.map((s) => s.key)).toEqual([
            "maestro-v1-v2-codex",
            "maestro-v1-v2-delegate",
            "n8n-vs-flow",
            "ete-weekly",
            "scribe",
        ]);
        expect(new Set(SERIES.map((s) => s.key)).size).toBe(SERIES.length);
        for (const s of SERIES) expect(GROUP_ORDER).toContain(s.group);
    });

    test("cadence, arms and source match each pipeline", () => {
        expect(seriesByKey("maestro-v1-v2-codex").cadence).toEqual([1, 2, 3, 4, 5]);
        expect(seriesByKey("maestro-v1-v2-delegate").cadence).toEqual([1]);
        expect(seriesByKey("n8n-vs-flow").cadence).toEqual([2]);
        expect(seriesByKey("ete-weekly").cadence).toEqual([1, 4, 6]);
        expect(seriesByKey("scribe").cadence).toEqual([1, 2, 3, 4, 5]);
        expect(seriesByKey("ete-weekly").arms).toEqual({ kind: "single" });
        expect(seriesByKey("scribe").source).toBe(SCRIBE_SOURCE);
        expect(seriesByKey("scribe").membership).toBe("canonical");
        for (const s of SERIES.filter((s) => s.key !== "scribe")) {
            expect(s.source).toBe(SKILLS_SOURCE);
            expect(s.membership).toBe("meta");
        }
    });

    test("n8n-vs-flow names the variant ids experiment.json carries", () => {
        const spec = seriesByKey("n8n-vs-flow").arms;
        expect(spec.kind === "variants" && spec.arms.map((a) => a.id)).toEqual([
            "n8n",
            "flow-v2",
        ]);
    });

    test("every entry has a one-sentence description and a schedule", () => {
        for (const s of SERIES) {
            expect(s.description.endsWith(".")).toBe(true);
            expect(s.schedule).not.toBe("");
        }
    });

    test("an unknown key gets a page labelled by the key, under Other", () => {
        const def = seriesByKey("some-new-series");
        expect(def.label).toBe("some-new-series");
        expect(def.group).toBe("Other");
        expect(def.membership).toBe("meta");
        expect(def.source).toBe(SKILLS_SOURCE);
        expect(def.arms).toEqual({ kind: "auto" });
    });

    test("isSeriesKey accepts path-safe keys only", () => {
        expect(isSeriesKey("maestro-v1-v2-codex")).toBe(true);
        expect(isSeriesKey("a.b_c")).toBe(true);
        expect(isSeriesKey("../x")).toBe(false);
        expect(isSeriesKey("a/b")).toBe(false);
        expect(isSeriesKey("")).toBe(false);
        expect(isSeriesKey("x".repeat(65))).toBe(false);
        expect(isSeriesKey(undefined)).toBe(false);
    });
});

describe("groupSeries", () => {
    test("groups the table in display order and drops empty groups", () => {
        expect(groupSeries().map((g) => g.group)).toEqual([
            "Maestro",
            "Automation Suite",
            "Autopilot",
        ]);
        expect(groupSeries()[0].series.map((s) => s.key)).toEqual([
            "maestro-v1-v2-codex",
            "maestro-v1-v2-delegate",
            "n8n-vs-flow",
        ]);
    });

    test("keys found only in data land under Other, sorted and deduplicated", () => {
        const groups = groupSeries(["zeta", "alpha", "zeta", "scribe"]);
        const other = groups[groups.length - 1];
        expect(other.group).toBe("Other");
        expect(other.series.map((s) => s.key)).toEqual(["alpha", "zeta"]);
        expect(groups.flatMap((g) => g.series).filter((s) => s.key === "scribe")).toHaveLength(1);
    });
});

describe("membership", () => {
    const codex = seriesByKey("maestro-v1-v2-codex");
    const scribe = seriesByKey("scribe");

    test("a meta series takes the runs whose meta.series is its key", () => {
        expect(isSeriesMember(codex, onDay(9))).toBe(true);
        expect(isSeriesMember(codex, onDay(9, { series: "n8n-vs-flow" }))).toBe(false);
        expect(isSeriesMember(codex, onDay(9, { series: null }))).toBe(false);
    });

    test("a meta series keeps its excluded runs as members", () => {
        expect(isSeriesMember(codex, onDay(9, { excluded: "infra" }))).toBe(true);
    });

    test("a canonical series takes the date-shaped, non-adhoc runs", () => {
        const canonical = ref({ id: "2026-11-09_03-00-00", dateShaped: true, adhoc: false });
        expect(isSeriesMember(scribe, canonical)).toBe(true);
        expect(isSeriesMember(scribe, { ...canonical, adhoc: true })).toBe(false);
        expect(isSeriesMember(scribe, ref({ id: "adhoc-2026-11-09_03-00-00" }))).toBe(false);
        expect(isSeriesMember(scribe, { ...canonical, adhoc: false, dateShaped: false })).toBe(false);
    });

    test("membersOf sorts newest first and ignores other series", () => {
        const runs = [
            onDay(5),
            onDay(9),
            onDay(6, { series: "n8n-vs-flow" }),
            onDay(7, { excluded: "infra" }),
        ];
        expect(membersOf(codex, runs).map((r) => r.id)).toEqual([
            onDay(9).id,
            onDay(7).id,
            onDay(5).id,
        ]);
    });

    test("plotted leaves out excluded runs", () => {
        const runs = [onDay(9), onDay(8, { excluded: "x" }), onDay(7)];
        expect(plotted(runs).map((r) => r.id)).toEqual([onDay(9).id, onDay(7).id]);
    });

    test("unlistedSeriesKeys reports keys the table lacks", () => {
        const runs = [
            onDay(9),
            onDay(8, { series: "brand-new" }),
            onDay(7, { series: "brand-new" }),
            onDay(6, { series: "another" }),
            onDay(5, { series: null }),
        ];
        expect(unlistedSeriesKeys(runs)).toEqual(["another", "brand-new"]);
    });
});

describe("excluded reasons", () => {
    test("a run id inside the reason is split out for linking", () => {
        expect(splitReason("replaced by adhoc-2026-11-10_08-12-03")).toEqual([
            { text: "replaced by " },
            { runId: "adhoc-2026-11-10_08-12-03" },
        ]);
    });

    test("a date-shaped id and trailing text are kept in order", () => {
        expect(splitReason("duplicate of 2026-11-04_20-19-37, see log")).toEqual([
            { text: "duplicate of " },
            { runId: "2026-11-04_20-19-37" },
            { text: ", see log" },
        ]);
    });

    test("a reason without a run id stays one text part", () => {
        expect(splitReason("infra: auth expired")).toEqual([
            { text: "infra: auth expired" },
        ]);
    });

    test("a bare date is not a run id", () => {
        expect(reasonRunIds("outage on 2026-11-09")).toEqual([]);
    });

    test("reasonRunIds lists every id", () => {
        expect(
            reasonRunIds("replaced by adhoc-2026-11-10_08-12-03 and adhoc-2026-11-10_09-00-00"),
        ).toEqual(["adhoc-2026-11-10_08-12-03", "adhoc-2026-11-10_09-00-00"]);
    });
});

describe("seriesHealth", () => {
    const weekdays = { cadence: [1, 2, 3, 4, 5] as const };
    // Tue 2026-11-10 09:00 UTC: the last five due weekdays are Nov 3 to Nov 9.
    const now = Date.UTC(2026, 10, 10, 9);

    function runs(days: number[]) {
        return days.map((d) => onDay(d));
    }

    test("on time when every due slot has a run", () => {
        const h = seriesHealth(weekdays, runs([2, 3, 4, 5, 6, 9, 10]), now);
        expect(h).toEqual({ status: "on-time", expected: 5 });
        expect(healthLabel(h)).toBe("on time");
    });

    test("counts the due slots with no run, out of the last five", () => {
        const h = seriesHealth(weekdays, runs([2, 3, 4, 6, 9]), now);
        expect(h).toEqual({ status: "missed", missed: 1, expected: 5 });
        expect(healthLabel(h)).toBe("missed 1 of last 5");
    });

    test("today's slot is not due until the day is over", () => {
        const h = seriesHealth(weekdays, runs([2, 3, 4, 5, 6, 9]), now);
        expect(h.status).toBe("on-time");
    });

    test("an excluded run does not cover its slot", () => {
        const all = runs([3, 4, 5, 6, 9]);
        all[1] = onDay(4, { excluded: "infra: auth expired" });
        expect(seriesHealth(weekdays, all, now)).toEqual({
            status: "missed",
            missed: 1,
            expected: 5,
        });
    });

    test("an excluded run covers its slot when the run that replaced it is live", () => {
        const replacement = onDay(10, { id: "adhoc-2026-11-10_08-12-03" });
        const all = [
            onDay(3),
            onDay(4, { excluded: `replaced by ${replacement.id}` }),
            onDay(5),
            onDay(6),
            onDay(9),
            replacement,
        ];
        expect(seriesHealth(weekdays, all, now).status).toBe("on-time");
    });

    test("a replacement that is itself excluded does not cover the slot", () => {
        const replacement = onDay(10, {
            id: "adhoc-2026-11-10_08-12-03",
            excluded: "infra",
        });
        const all = [
            onDay(3),
            onDay(4, { excluded: `replaced by ${replacement.id}` }),
            onDay(5),
            onDay(6),
            onDay(9),
            replacement,
        ];
        expect(seriesHealth(weekdays, all, now)).toMatchObject({
            status: "missed",
            missed: 1,
        });
    });

    test("slots before the series' first run are not expected", () => {
        const h = seriesHealth(weekdays, runs([6, 9]), now);
        expect(h).toEqual({ status: "on-time", expected: 2 });
    });

    test("a weekly series looks back over its own slots", () => {
        const mondays = { cadence: [1] as const };
        const weeks = [
            Date.UTC(2026, 9, 12, 19),
            Date.UTC(2026, 9, 26, 19),
            Date.UTC(2026, 10, 2, 19),
        ].map((startMs, i) => onDay(1, { id: `adhoc-w${i}`, startMs }));
        const h = seriesHealth(mondays, weeks, Date.UTC(2026, 10, 10, 9));
        expect(h).toEqual({ status: "missed", missed: 2, expected: 5 });
    });

    test("paused, no runs and no schedule never read as missed", () => {
        expect(seriesHealth({ ...weekdays, paused: true }, [], now)).toEqual({
            status: "paused",
        });
        expect(seriesHealth(weekdays, [], now)).toEqual({ status: "no-runs" });
        expect(seriesHealth({ cadence: [] }, runs([9]), now)).toEqual({
            status: "unscheduled",
        });
        expect(healthLabel({ status: "paused" })).toBe("paused");
        expect(healthLabel({ status: "no-runs" })).toBe("no runs");
        expect(healthLabel({ status: "unscheduled" })).toBe("no schedule");
    });

    test("the window is capped at the requested number of slots", () => {
        const h = seriesHealth(weekdays, [onDay(2)], Date.UTC(2026, 10, 30), 3);
        expect(h).toEqual({ status: "missed", missed: 3, expected: 3 });
    });
});

describe("arms and values", () => {
    const v1v2 = seriesByKey("maestro-v1-v2-codex").arms;
    const n8n = seriesByKey("n8n-vs-flow").arms;

    test("a single series has the one arm", () => {
        expect(resolveArms({ kind: "single" }, ["x"])).toEqual([SINGLE_ARM]);
    });

    test("declared arms keep their order and label", () => {
        expect(resolveArms(n8n, ["flow-v2", "n8n"])).toEqual([
            { id: "n8n", label: "n8n" },
            { id: "flow-v2", label: "Flow" },
        ]);
    });

    test("before any run is read the declared arms stand in", () => {
        expect(resolveArms(v1v2, [])).toEqual([
            { id: "v1", label: "v1" },
            { id: "v2", label: "v2" },
        ]);
    });

    test("an undeclared id is added under its own id; an absent declared arm is dropped", () => {
        expect(resolveArms(v1v2, ["v2", "v3"])).toEqual([
            { id: "v2", label: "v2" },
            { id: "v3", label: "v3" },
        ]);
    });

    test("an auto series takes the ids the runs carry", () => {
        expect(resolveArms({ kind: "auto" }, ["b", "a", "b"])).toEqual([
            { id: "a", label: "a" },
            { id: "b", label: "b" },
        ]);
        expect(resolveArms({ kind: "auto" }, [])).toEqual([SINGLE_ARM]);
        expect(resolveArms({ kind: "auto" }, [SINGLE_ARM.id])).toEqual([SINGLE_ARM]);
    });

    const arms = [
        { id: "v1", label: "v1" },
        { id: "v2", label: "v2" },
    ];

    test("latest takes the newest point, null where an arm is missing", () => {
        const points: SeriesPoint[] = [
            { id: "b", startMs: 2, values: { v1: 81, v2: null } },
            { id: "a", startMs: 1, values: { v1: 70, v2: 72 } },
        ];
        expect(latestValues(points, arms)).toEqual({ v1: 81, v2: null });
        expect(latestValues([], arms)).toEqual({ v1: null, v2: null });
    });

    test("the gap is the second arm minus the first, only for two arms", () => {
        expect(armGap({ v1: 81, v2: 84 }, arms)).toBe(3);
        expect(armGap({ v1: 81, v2: null }, arms)).toBeNull();
        expect(armGap({ all: 70 }, [SINGLE_ARM])).toBeNull();
        expect(armGap({ a: 1, b: 2, c: 3 }, [...arms, { id: "c", label: "c" }])).toBeNull();
    });

    test("formatGap signs the rounded gap and names the second arm", () => {
        expect(formatGap(2.6, arms[1])).toBe("v2 +3");
        expect(formatGap(-2.6, arms[1])).toBe("v2 -3");
        expect(formatGap(0.2, arms[1])).toBe("v2 ±0");
    });

    test("trendRows is oldest first, keyed by position, and skips empty points", () => {
        const points: SeriesPoint[] = [
            { id: "c", startMs: 3, values: { v1: 80, v2: 84 } },
            { id: "b", startMs: 2, values: { v1: null, v2: null } },
            { id: "a", startMs: 1, values: { v1: 70, v2: null } },
        ];
        const { rows, lines } = trendRows(points, arms);
        expect(rows).toEqual([
            { timestamp: 1, a0: 70 },
            { timestamp: 3, a0: 80, a1: 84 },
        ]);
        expect(lines.map((l) => [l.key, l.label])).toEqual([
            ["a0", "v1"],
            ["a1", "v2"],
        ]);
        expect(lines[0].color).not.toBe(lines[1].color);
    });

    test("gapRows has one row per point with both arms", () => {
        const points: SeriesPoint[] = [
            { id: "b", startMs: 2, values: { v1: 80, v2: 77 } },
            { id: "a", startMs: 1, values: { v1: 70, v2: null } },
        ];
        expect(gapRows(points, arms)).toEqual([{ timestamp: 2, gap: -3 }]);
    });
});

describe("parseMetric", () => {
    test("defaults to pass rate and accepts the other metrics", () => {
        expect(parseMetric(undefined)).toBe("pass");
        expect(parseMetric("bogus")).toBe("pass");
        expect(parseMetric("cost")).toBe("cost");
        expect(parseMetric(["wall", "cost"])).toBe("wall");
        expect(parseMetric("errors")).toBe("errors");
    });
});

describe("sparkline", () => {
    test("the domain widens a flat series so small moves show", () => {
        expect(sparkDomain([80, 82])).toEqual([76, 86]);
        expect(sparkDomain([10, 90])).toEqual([10, 90]);
        expect(sparkDomain([])).toEqual([0, 100]);
    });

    test("the domain stays inside 0 to 100", () => {
        const [lo, hi] = sparkDomain([98, 100]);
        expect(hi).toBe(100);
        expect(lo).toBe(90);
        const [lo2, hi2] = sparkDomain([0, 2]);
        expect(lo2).toBe(0);
        expect(hi2).toBe(10);
    });

    test("a null breaks the line and the last point is reported", () => {
        const { d, last } = sparkLine([0, 100, null, 50], 100, 20, [0, 100], 0);
        expect(d).toBe("M0.0 20.0 L33.3 0.0 M100.0 10.0");
        expect(last).toEqual({ x: 100, y: 10 });
    });

    test("no values give an empty path", () => {
        expect(sparkLine([null, null], 100, 20, [0, 100])).toEqual({ d: "", last: null });
    });

    test("one value draws a point only", () => {
        const { d, last } = sparkLine([50], 100, 20, [0, 100], 2);
        expect(d).toBe("M2.0 10.0");
        expect(last).toEqual({ x: 2, y: 10 });
    });
});
