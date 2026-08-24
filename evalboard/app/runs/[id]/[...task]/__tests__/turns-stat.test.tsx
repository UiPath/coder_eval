import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { ExpectedTurnsStat, TurnsStat } from "../turns-stat";

function renderTurns(turns: number | null, expected: number | null) {
    return render(
        <dl>
            <TurnsStat turns={turns} expectedTurns={expected} />
        </dl>,
    );
}

describe("TurnsStat", () => {
    test("red text and target tooltip when > +50% (ratio 2.4)", () => {
        renderTurns(12, 5);
        const dd = screen.getByText("12");
        expect(dd.tagName).toBe("DD");
        expect(dd.className).toContain("text-rose-700");
        expect(dd.className).not.toContain("bg-");
        expect(dd).toHaveAttribute("title", "expected_turns target: 5");
    });

    test("yellow text at +25%–+50% (ratio 1.4)", () => {
        renderTurns(7, 5);
        const dd = screen.getByText("7");
        expect(dd.className).toContain("text-amber-700");
    });

    test("green text at or under +25% (ratio 1.2)", () => {
        renderTurns(6, 5);
        const dd = screen.getByText("6");
        expect(dd.className).toContain("text-emerald-700");
    });

    test("black-ish default when no target", () => {
        renderTurns(7, null);
        const dd = screen.getByText("7");
        expect(dd.tagName).toBe("DD");
        expect(dd.className).toContain("text-gray-900");
        expect(dd.className).not.toMatch(/text-(rose|amber|emerald)-/);
        expect(dd).toHaveAttribute("title", "no expected_turns target set");
    });

    test("both null renders em dash with default text", () => {
        renderTurns(null, null);
        const dd = screen.getByText("—");
        expect(dd.tagName).toBe("DD");
        expect(dd.className).toContain("text-gray-900");
        expect(dd).toHaveAttribute("title", "no expected_turns target set");
    });
});

describe("ExpectedTurnsStat", () => {
    test("renders the target when set", () => {
        render(
            <dl>
                <ExpectedTurnsStat expectedTurns={12} />
            </dl>,
        );
        expect(screen.getByText("Expected turns")).toBeInTheDocument();
        expect(screen.getByText("12")).toBeInTheDocument();
    });

    test("renders em dash when unset", () => {
        render(
            <dl>
                <ExpectedTurnsStat expectedTurns={null} />
            </dl>,
        );
        expect(screen.getByText("—")).toBeInTheDocument();
    });
});
