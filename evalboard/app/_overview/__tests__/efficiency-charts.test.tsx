import { describe, expect, test } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { RunPoint } from "@/lib/overview";
import { EfficiencyCharts } from "../efficiency-charts";

function point(overrides: Partial<RunPoint> = {}): RunPoint {
    return {
        runId: "2026-08-18_04-51-58",
        timestamp: Date.UTC(2026, 7, 18),
        harness: "codex",
        successRate: 96,
        turnBudgetRate: 87,
        withinExpectedTimeRate: 78,
        timePerPassedTask: 192,
        ...overrides,
    };
}

function renderCharts(scoped = false) {
    return render(
        <EfficiencyCharts
            data={[point()]}
            harnesses={["codex"]}
            windowStart={Date.UTC(2026, 7, 1)}
            windowEnd={Date.UTC(2026, 7, 31)}
            scoped={scoped}
        />,
    );
}

describe("EfficiencyCharts", () => {
    test("opens on the wall-clock metric", () => {
        renderCharts();
        expect(screen.getByRole("heading")).toHaveTextContent(
            "Time per Passed Task",
        );
        expect(
            screen.getByRole("tab", { name: "Time" }),
        ).toHaveAttribute("aria-selected", "true");
    });

    test("the Turns tab still reaches the retired turn-budget chart", () => {
        // The turn budget is kept visible while the derived expected-time line
        // is being watched; retiring it is deleting this tab.
        renderCharts();
        fireEvent.click(screen.getByRole("tab", { name: "Turns" }));
        expect(screen.getByRole("heading")).toHaveTextContent(
            "Within Expected Turns (%)",
        );
        expect(screen.getByText(/1.5× their expected turns/)).toBeInTheDocument();
        expect(
            screen.getByRole("tab", { name: "Turns" }),
        ).toHaveAttribute("aria-selected", "true");
        expect(screen.getByRole("tab", { name: "Time" })).toHaveAttribute(
            "aria-selected",
            "false",
        );
    });

    test("switching back restores the wall-clock blurb", () => {
        renderCharts();
        fireEvent.click(screen.getByRole("tab", { name: "Turns" }));
        fireEvent.click(screen.getByRole("tab", { name: "Time" }));
        expect(
            screen.getByText(/the number that passed/),
        ).toBeInTheDocument();
    });

    test("says so when the numbers are filter-scoped", () => {
        renderCharts(true);
        expect(
            screen.getByText(/scoped to the active filter/),
        ).toBeInTheDocument();
    });

    test("no filter note when the window is unscoped", () => {
        renderCharts();
        expect(screen.queryByText(/scoped to the active filter/)).toBeNull();
    });
});
