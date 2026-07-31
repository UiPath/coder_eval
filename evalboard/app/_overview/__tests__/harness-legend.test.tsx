import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { harnessColor } from "@/lib/harness";
import { HarnessLegend, HarnessTooltip } from "../harness-legend";
import { seriesKey } from "../harness-series";

function series(...harnesses: string[]) {
    return harnesses.map((harness, i) => ({
        harness,
        dataKey: seriesKey(harness, i),
        color: harnessColor(harness),
    }));
}

describe("HarnessLegend", () => {
    test("names every series, so identity is never color-alone", () => {
        render(<HarnessLegend series={series("claude-code", "codex")} />);
        expect(screen.getByText("Claude Code")).toBeInTheDocument();
        expect(screen.getByText("Codex")).toBeInTheDocument();
    });

    test("renders nothing for a single series", () => {
        // A one-line chart is already named by its own heading; a legend box
        // there is noise.
        const { container } = render(
            <HarnessLegend series={series("claude-code")} />,
        );
        expect(container).toBeEmptyDOMElement();
    });

    test("renders nothing when there is no data at all", () => {
        const { container } = render(<HarnessLegend series={[]} />);
        expect(container).toBeEmptyDOMElement();
    });
});

describe("HarnessTooltip", () => {
    const s = series("claude-code", "codex");

    test("shows only the harnesses that actually ran at the hovered x", () => {
        // Recharts hands over every series, including the ones with no value
        // here. Listing those would invent runs that never happened.
        render(
            <HarnessTooltip
                active
                label={1_700_000_000_000}
                series={s}
                suffix="success"
                emptyText="no tasks"
                payload={[
                    { dataKey: s[0].dataKey, value: 92.5 },
                    { dataKey: s[1].dataKey, value: undefined },
                ]}
            />,
        );
        expect(screen.getByText("Claude Code")).toBeInTheDocument();
        expect(screen.getByText("92.5% success")).toBeInTheDocument();
        expect(screen.queryByText("Codex")).not.toBeInTheDocument();
    });

    test("keeps a real zero instead of filtering it out as falsy", () => {
        render(
            <HarnessTooltip
                active
                label={1_700_000_000_000}
                series={s}
                suffix="success"
                emptyText="no tasks"
                payload={[{ dataKey: s[0].dataKey, value: 0 }]}
            />,
        );
        expect(screen.getByText("0.0% success")).toBeInTheDocument();
    });

    test("falls back to the empty text when no series has a value", () => {
        render(
            <HarnessTooltip
                active
                label={1_700_000_000_000}
                series={s}
                suffix="within turn budget"
                emptyText="no tasks with a turn budget"
                payload={[{ dataKey: s[0].dataKey, value: undefined }]}
            />,
        );
        expect(
            screen.getByText("no tasks with a turn budget"),
        ).toBeInTheDocument();
    });

    test("ignores payload entries for unknown series keys", () => {
        render(
            <HarnessTooltip
                active
                label={1_700_000_000_000}
                series={s}
                suffix="success"
                emptyText="no tasks"
                payload={[{ dataKey: "h_stale_key", value: 50 }]}
            />,
        );
        expect(screen.getByText("no tasks")).toBeInTheDocument();
    });

    test("renders nothing while inactive", () => {
        const { container } = render(
            <HarnessTooltip
                series={s}
                suffix="success"
                emptyText="no tasks"
                payload={[{ dataKey: s[0].dataKey, value: 1 }]}
            />,
        );
        expect(container).toBeEmptyDOMElement();
    });

    test("renders the UTC timestamp header from the x label", () => {
        render(
            <HarnessTooltip
                active
                label={Date.UTC(2026, 6, 30, 4, 27)}
                series={s}
                suffix="success"
                emptyText="no tasks"
                payload={[{ dataKey: s[0].dataKey, value: 80 }]}
            />,
        );
        expect(screen.getByText("2026-07-30 04:27 UTC")).toBeInTheDocument();
    });
});
