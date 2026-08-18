import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { DurationStat, ExpectedTimeStat, TurnsStat } from "../task-stats";

function renderDuration(seconds: number | null, expected: number | null) {
    return render(
        <dl>
            <DurationStat durationSeconds={seconds} expectedSeconds={expected} />
        </dl>,
    );
}

describe("DurationStat", () => {
    test("red ratio beside the time when > 2x expected (ratio 2.4)", () => {
        renderDuration(240, 100);
        const dd = screen.getByText("4m00s");
        expect(dd.tagName).toBe("DD");
        const ratio = screen.getByText("2.4×");
        expect(ratio.className).toContain("text-rose-700");
        expect(ratio.className).not.toContain("bg-");
        expect(dd).toHaveAttribute(
            "title",
            "2.40x expected · expected time: 1m40s",
        );
    });

    test("yellow ratio between 1.5x and 2x expected (ratio 1.8)", () => {
        renderDuration(180, 100);
        expect(screen.getByText("1.8×").className).toContain("text-amber-700");
    });

    test("green ratio at or under 1.5x expected (ratio 1.2)", () => {
        renderDuration(120, 100);
        expect(screen.getByText("1.2×").className).toContain(
            "text-emerald-700",
        );
    });

    test("the time itself is never tinted, so the ratio carries the signal", () => {
        renderDuration(240, 100);
        const dd = screen.getByText("4m00s");
        expect(dd.className).toContain("text-gray-900");
        expect(dd.className).not.toMatch(/text-(rose|amber|emerald)-/);
    });

    test("an unscored task shows no ratio and says why", () => {
        renderDuration(120, null);
        const dd = screen.getByText("2m00s");
        expect(dd.className).toContain("text-gray-900");
        expect(screen.queryByText(/×$/)).toBeNull();
        expect(dd).toHaveAttribute(
            "title",
            "no expected time yet (needs a passing run on this harness)",
        );
    });

    test("both null renders em dash with default text", () => {
        renderDuration(null, null);
        const dd = screen.getByText("—");
        expect(dd.tagName).toBe("DD");
        expect(dd.className).toContain("text-gray-900");
    });
});

describe("ExpectedTimeStat", () => {
    test("renders the derived line when the task is scored", () => {
        render(
            <dl>
                <ExpectedTimeStat expectedSeconds={104} />
            </dl>,
        );
        expect(screen.getByText("Expected time")).toBeInTheDocument();
        expect(screen.getByText("1m44s")).toBeInTheDocument();
    });

    test("renders em dash when the task is unscored", () => {
        render(
            <dl>
                <ExpectedTimeStat expectedSeconds={null} />
            </dl>,
        );
        expect(screen.getByText("—")).toBeInTheDocument();
    });
});

describe("TurnsStat", () => {
    test("renders a plain count, never tinted", () => {
        render(
            <dl>
                <TurnsStat turns={12} />
            </dl>,
        );
        const dd = screen.getByText("12");
        expect(dd.tagName).toBe("DD");
        expect(dd.className).toContain("text-gray-900");
        expect(dd.className).not.toMatch(/text-(rose|amber|emerald)-/);
    });

    test("renders em dash when there is nothing to count", () => {
        render(
            <dl>
                <TurnsStat turns={null} />
            </dl>,
        );
        expect(screen.getByText("—")).toBeInTheDocument();
    });
});
