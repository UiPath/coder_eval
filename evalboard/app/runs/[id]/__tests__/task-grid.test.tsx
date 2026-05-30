import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { TaskResultSummary } from "@/lib/runs";
import { TaskGrid } from "../task-grid";

function row(
    taskId: string,
    actualCommands: number | null,
    expectedTurns: number | null,
): TaskResultSummary {
    return {
        taskId,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands,
        totalTurns: null,
        expectedTurns,
        hasFinalReply: false,
        outputTokens: null,
        cacheCreationTokens: null,
        cacheReadTokens: null,
        tags: [],
        skill: null,
    };
}

function turnsCellFor(taskId: string): HTMLElement {
    const link = screen.getByRole("link", { name: new RegExp(taskId, "i") });
    const tr = link.closest("tr")!;
    const cells = within(tr).getAllByRole("cell");
    // Layout: Task, Status, Score, Duration, Cost, Turns, Out, Cache+, Cache↺
    return cells[5]!;
}

describe("TaskGrid — Turns column", () => {
    test("colorizes the digits per ratio bucket (no background)", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("over", 10, 5), // ratio 2.0 → red (> 1.5)
                    row("mid", 7, 5), // ratio 1.4 → yellow (1.25 < r ≤ 1.5)
                    row("under", 4, 10), // ratio 0.4 → green (≤ 1.25)
                    row("notarget", 7, null), // black-ish default
                ]}
            />,
        );

        const overCell = turnsCellFor("over");
        expect(overCell).toHaveTextContent("10");
        expect(overCell.className).toContain("text-rose-700");
        expect(overCell.className).not.toContain("bg-");
        expect(overCell).toHaveAttribute(
            "title",
            "expected_turns target: 5",
        );

        const midCell = turnsCellFor("mid");
        expect(midCell.className).toContain("text-amber-700");

        const underCell = turnsCellFor("under");
        expect(underCell.className).toContain("text-emerald-700");

        const noTargetCell = turnsCellFor("notarget");
        expect(noTargetCell).toHaveTextContent("7");
        expect(noTargetCell.className).toContain("text-gray-900");
        expect(noTargetCell.className).not.toMatch(
            /text-(rose|amber|emerald)-/,
        );
        expect(noTargetCell).toHaveAttribute(
            "title",
            "no expected_turns target set",
        );
    });

    test("renders em dash when actualCommands is null", () => {
        render(<TaskGrid runId="r1" tasks={[row("legacy", null, null)]} />);
        const cell = turnsCellFor("legacy");
        expect(cell).toHaveTextContent("—");
        expect(cell.className).toContain("text-gray-900");
    });

    test("table columns include perf + token stats", () => {
        render(<TaskGrid runId="r1" tasks={[row("x", 1, 1)]} />);
        const headers = screen.getAllByRole("columnheader");
        // Read the sort toggle (first button) per header — token columns also
        // carry an ⓘ help button, so the bare th textContent isn't the label.
        const labels = headers.map(
            (h) => within(h).getAllByRole("button")[0].textContent?.trim() ?? "",
        );
        expect(labels).toEqual([
            "Task",
            "Status",
            "Score",
            "Duration",
            "Cost",
            "Turns",
            "Out",
            "Cache+",
            "Cache↺",
        ]);
    });
});

describe("TaskGrid — column help popover", () => {
    test("ⓘ toggles a static help card; Escape closes it", () => {
        render(<TaskGrid runId="r1" tasks={[row("x", 1, 1)]} />);
        const trigger = screen.getByRole("button", {
            name: /What is Cache↺/i,
        });

        expect(screen.queryByRole("tooltip")).toBeNull();

        fireEvent.click(trigger);
        const card = screen.getByRole("tooltip");
        expect(card).toHaveTextContent("Cache-read tokens");
        expect(card).toHaveTextContent("Common causes:");
        expect(card).toHaveTextContent("Reduce by:");

        fireEvent.keyDown(document, { key: "Escape" });
        expect(screen.queryByRole("tooltip")).toBeNull();
    });

    test("opening one column's help closes another's", () => {
        render(<TaskGrid runId="r1" tasks={[row("x", 1, 1)]} />);
        fireEvent.click(screen.getByRole("button", { name: /What is Out/i }));
        expect(screen.getByRole("tooltip")).toHaveTextContent("Output tokens");

        fireEvent.click(
            screen.getByRole("button", { name: /What is Cache↺/i }),
        );
        const card = screen.getByRole("tooltip");
        expect(card).toHaveTextContent("Cache-read tokens");
        expect(card).not.toHaveTextContent("Output tokens");
    });
});
