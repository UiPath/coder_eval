import { describe, expect, test, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { SeriesIndexRow, SeriesDetailRun } from "@/lib/series-data";
import { seriesByKey, SINGLE_ARM } from "@/lib/series";

vi.mock("next/cache", () => ({
    unstable_cache: <T extends (...a: never[]) => unknown>(fn: T) => fn,
}));

vi.mock("../[key]/run-cost", () => ({
    RunCost: ({ id }: { id: string }) => <span>cost of {id}</span>,
}));

const { ReasonText } = await import("../_components/reason-text");
const { HealthPill } = await import("../_components/health-pill");
const { Sparkline } = await import("../_components/sparkline");
const { LatestCell } = await import("../_components/series-row");
const { TrendChart } = await import("../[key]/trend-chart");
const { RunList } = await import("../[key]/run-list");

const V1V2 = [
    { id: "v1", label: "v1" },
    { id: "v2", label: "v2" },
];

function indexRow(over: Partial<SeriesIndexRow>): SeriesIndexRow {
    return {
        def: seriesByKey("maestro-v1-v2-codex"),
        arms: V1V2,
        runCount: 3,
        lastRun: null,
        health: { status: "on-time", expected: 5 },
        latest: { v1: 81.2, v2: 84.4 },
        gap: 3.2,
        sparks: {},
        ...over,
    };
}

describe("ReasonText", () => {
    test("links a run id in the reason to its run page", () => {
        render(
            <ReasonText
                reason="replaced by adhoc-2026-11-10_08-12-03"
                sourceId="skills"
            />,
        );
        const link = screen.getByRole("link", { name: "adhoc-2026-11-10_08-12-03" });
        expect(link).toHaveAttribute("href", "/runs/adhoc-2026-11-10_08-12-03");
        expect(screen.getByText(/replaced by/)).toBeInTheDocument();
    });

    test("carries the source for a run in another container", () => {
        render(<ReasonText reason="see 2026-11-04_20-19-37" sourceId="scribe" />);
        expect(screen.getByRole("link")).toHaveAttribute(
            "href",
            "/runs/2026-11-04_20-19-37?src=scribe",
        );
    });

    test("a reason with no run id renders as plain text", () => {
        render(<ReasonText reason="infra: auth expired" sourceId="skills" />);
        expect(screen.queryByRole("link")).toBeNull();
        expect(screen.getByText("infra: auth expired")).toBeInTheDocument();
    });
});

describe("HealthPill", () => {
    test("says on time, or how many of the last slots were missed", () => {
        const { rerender } = render(
            <HealthPill health={{ status: "on-time", expected: 5 }} />,
        );
        expect(screen.getByText("on time")).toBeInTheDocument();
        rerender(<HealthPill health={{ status: "missed", missed: 1, expected: 5 }} />);
        expect(screen.getByText("missed 1 of last 5")).toBeInTheDocument();
    });
});

describe("LatestCell", () => {
    test("shows each arm's rate and the gap for two arms", () => {
        render(<LatestCell row={indexRow({})} />);
        expect(screen.getByText(/81%/)).toBeInTheDocument();
        expect(screen.getByText(/84%/)).toBeInTheDocument();
        expect(screen.getByText("(v2 +3)")).toBeInTheDocument();
    });

    test("a negative gap is signed", () => {
        render(<LatestCell row={indexRow({ gap: -2.8 })} />);
        expect(screen.getByText("(v2 -3)")).toBeInTheDocument();
    });

    test("a single arm shows the one rate", () => {
        const { container } = render(
            <LatestCell
                row={indexRow({
                    arms: [SINGLE_ARM],
                    latest: { all: 74 },
                    gap: null,
                })}
            />,
        );
        expect(container).toHaveTextContent("74%");
        expect(container).not.toHaveTextContent("(");
    });

    test("an arm with no value reads as a dash", () => {
        const { container } = render(
            <LatestCell row={indexRow({ latest: { v1: 80, v2: null }, gap: null })} />,
        );
        expect(container).toHaveTextContent("v2 —");
    });
});

describe("Sparkline", () => {
    test("draws one path per arm", () => {
        const { container } = render(
            <Sparkline
                arms={V1V2}
                sparks={{ v1: [70, 75, 80], v2: [72, null, 84] }}
            />,
        );
        expect(screen.getByRole("img")).toBeInTheDocument();
        expect(container.querySelectorAll("path")).toHaveLength(2);
        expect(container.querySelectorAll("circle")).toHaveLength(2);
    });

    test("renders a dash when there is nothing to draw", () => {
        const { container } = render(
            <Sparkline arms={V1V2} sparks={{ v1: [], v2: [null] }} />,
        );
        expect(container.querySelector("svg")).toBeNull();
        expect(container).toHaveTextContent("—");
    });
});

describe("TrendChart", () => {
    const rows = [
        { timestamp: Date.UTC(2026, 10, 5), a0: 70, a1: 72 },
        { timestamp: Date.UTC(2026, 10, 6), a0: 75, a1: 80 },
    ];

    test("a legend names every arm of a multi-line chart", () => {
        render(
            <TrendChart
                rows={rows}
                lines={[
                    { key: "a0", label: "v1", color: "#6b7280" },
                    { key: "a1", label: "v2", color: "#1976D2" },
                ]}
                unit="percent"
            />,
        );
        expect(screen.getByText("v1")).toBeInTheDocument();
        expect(screen.getByText("v2")).toBeInTheDocument();
    });

    test("a single line has no legend", () => {
        render(
            <TrendChart
                rows={rows}
                lines={[{ key: "a0", label: "Pass rate", color: "#1976D2" }]}
                unit="percent"
            />,
        );
        expect(screen.queryByText("Pass rate")).toBeNull();
    });
});

describe("RunList", () => {
    const def = seriesByKey("maestro-v1-v2-codex");
    const stat = (passRate: number, passed: number, graded: number) => ({
        passRate,
        passed,
        graded,
        errors: 0,
    });
    const runs: SeriesDetailRun[] = [
        {
            ref: {
                id: "adhoc-2026-11-10_08-12-03",
                startMs: Date.UTC(2026, 10, 10, 8, 12),
                dateShaped: false,
                adhoc: true,
                title: "Maestro v1 vs v2 · codex · ADO 20261110.2",
                series: def.key,
                excluded: null,
            },
            stats: { id: "a", arms: { v1: stat(81, 81, 100), v2: stat(84, 84, 100) } },
        },
        {
            ref: {
                id: "adhoc-2026-11-09_16-18-40",
                startMs: Date.UTC(2026, 10, 9, 16, 18),
                dateShaped: false,
                adhoc: true,
                title: null,
                series: def.key,
                excluded: "replaced by adhoc-2026-11-10_08-12-03",
            },
            stats: { id: "b", arms: { v1: stat(41, 41, 100), v2: stat(44, 44, 100) } },
        },
    ];

    function renderList(moreHref: string | null = null) {
        return render(
            <RunList def={def} arms={V1V2} runs={runs} total={5} moreHref={moreHref} />,
        );
    }

    test("an excluded run is marked, greyed, and its reason links to the replacement", () => {
        const { container } = renderList();
        const excluded = container.querySelector('tr[data-excluded="true"]') as HTMLElement;
        expect(excluded).not.toBeNull();
        expect(excluded.className).toContain("text-gray-400");
        expect(within(excluded).getByText("Excluded")).toBeInTheDocument();
        expect(
            within(excluded).getByRole("link", { name: "adhoc-2026-11-10_08-12-03" }),
        ).toHaveAttribute("href", "/runs/adhoc-2026-11-10_08-12-03");
    });

    test("a plotted run is not greyed and carries no reason", () => {
        const { container } = renderList();
        expect(container.querySelectorAll('tr[data-excluded="true"]')).toHaveLength(1);
        expect(screen.getAllByText("Excluded")).toHaveLength(1);
    });

    test("each arm's rate and counts are shown, with the run count out of the total", () => {
        const { container } = renderList();
        expect(container).toHaveTextContent("v1 81%81/100");
        expect(container).toHaveTextContent("v2 84%84/100");
        expect(container).toHaveTextContent("2 of 5");
    });

    test("a Show more link appears only when more runs exist", () => {
        const { unmount } = renderList("/series/maestro-v1-v2-codex?limit=60");
        expect(screen.getByRole("link", { name: "Show more" })).toHaveAttribute(
            "href",
            "/series/maestro-v1-v2-codex?limit=60",
        );
        unmount();
        renderList(null);
        expect(screen.queryByRole("link", { name: "Show more" })).toBeNull();
    });

    test("each row has a cost cell for its own run", () => {
        renderList();
        expect(screen.getByText("cost of adhoc-2026-11-10_08-12-03")).toBeInTheDocument();
        expect(screen.getByText("cost of adhoc-2026-11-09_16-18-40")).toBeInTheDocument();
    });

    test("a run without numbers says so", () => {
        render(
            <RunList
                def={def}
                arms={V1V2}
                runs={[{ ...runs[0], stats: null }]}
                total={1}
                moreHref={null}
            />,
        );
        expect(screen.getByText("no data")).toBeInTheDocument();
    });
});
