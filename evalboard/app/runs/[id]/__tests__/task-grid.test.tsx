import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { TaskResultSummary } from "@/lib/runs";
import { TaskGrid } from "../task-grid";

function row(
    taskId: string,
    actualCommands: number | null,
    expectedTurns: number | null,
    extra: Partial<TaskResultSummary> = {},
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
        inputTokens: null,
        outputTokens: null,
        cacheCreationTokens: null,
        cacheReadTokens: null,
        model: null,
        tags: [],
        skill: null,
        ...extra,
    };
}

// Token columns (Cache R / Cache W / Out) are collapsed by default on every
// screen now — click the toolbar toggle to reveal them before asserting on them.
function revealTokens(): void {
    fireEvent.click(screen.getByRole("button", { name: /show tokens/i }));
}

function turnsCellFor(taskId: string): HTMLElement {
    // Scope to the desktop <table>: below md the grid also renders each task as
    // a card (same link/values), so an unscoped query would match twice.
    const table = screen.getByRole("table");
    const link = within(table).getByRole("link", {
        name: new RegExp(taskId, "i"),
    });
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

    test("token columns are collapsed by default, revealed by the toggle", () => {
        render(<TaskGrid runId="r1" tasks={[row("x", 1, 1)]} />);
        // Read the sort toggle (first button) per header — token columns also
        // carry an ⓘ help button, so the bare th textContent isn't the label.
        const labels = () =>
            screen
                .getAllByRole("columnheader")
                .map(
                    (h) =>
                        within(h).getAllByRole("button")[0].textContent?.trim() ??
                        "",
                );
        // Default: the six essentials only, no token group.
        expect(labels()).toEqual([
            "Task",
            "Status",
            "Score",
            "Duration",
            "Cost",
            "Turns",
        ]);
        // Toggle reveals the perf + token columns.
        revealTokens();
        expect(labels()).toEqual([
            "Task",
            "Status",
            "Score",
            "Duration",
            "Cost",
            "Turns",
            "In",
            "Cache R",
            "Cache W",
            "Out",
        ]);
    });
});

describe("TaskGrid — column help popover", () => {
    test("ⓘ toggles a static help card; Escape closes it", () => {
        render(<TaskGrid runId="r1" tasks={[row("x", 1, 1)]} />);
        revealTokens(); // ⓘ help buttons live on the token columns
        const trigger = screen.getByRole("button", {
            name: /What is Cache R/i,
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
        revealTokens(); // ⓘ help buttons live on the token columns
        fireEvent.click(screen.getByRole("button", { name: /What is Out/i }));
        expect(screen.getByRole("tooltip")).toHaveTextContent("Output tokens");

        fireEvent.click(
            screen.getByRole("button", { name: /What is Cache R/i }),
        );
        const card = screen.getByRole("tooltip");
        expect(card).toHaveTextContent("Cache-read tokens");
        expect(card).not.toHaveTextContent("Output tokens");
    });
});

describe("TaskGrid — dataset-expanded task links", () => {
    test("link href uses the full slash-separated task ID", () => {
        render(
            <TaskGrid
                runId="2026-06-03_16-16-26"
                tasks={[row("sentiment-classification/r3", 1, null)]}
            />,
        );
        // humanizeTaskId replaces dashes with spaces: "Sentiment classification/r3"
        // Scope to the table — the mobile card renders the same link.
        const link = within(screen.getByRole("table")).getByRole("link", {
            name: /sentiment classification/i,
        });
        expect(link).toHaveAttribute(
            "href",
            "/runs/2026-06-03_16-16-26/sentiment-classification/r3",
        );
    });
});

describe("TaskGrid — Tokens↔USD toggle", () => {
    const priced = row("x", 1, 1, {
        model: "claude-sonnet-4-6",
        outputTokens: 2000,
        cacheCreationTokens: 1000,
        cacheReadTokens: 80_000,
    });

    test("shows token counts once revealed, no 'estimated' badge", () => {
        render(<TaskGrid runId="r1" tasks={[priced]} />);
        revealTokens();
        // Scope value lookups to the table — the mobile card duplicates them.
        const table = screen.getByRole("table");
        expect(within(table).getByText("2k")).toBeInTheDocument();
        expect(screen.queryByText(/estimated/i)).toBeNull();
    });

    test("USD mode prices each bucket and shows an 'estimated' badge", () => {
        render(<TaskGrid runId="r1" tasks={[priced]} />);
        revealTokens();
        fireEvent.click(screen.getByRole("button", { name: "USD" }));
        const table = screen.getByRole("table");
        // output: 2000 · 15 / 1e6 = 0.03 → "$0.0300"
        expect(within(table).getByText("$0.0300")).toBeInTheDocument();
        // cache-read: 80000 · 0.3 / 1e6 = 0.024 → "$0.0240"
        expect(within(table).getByText("$0.0240")).toBeInTheDocument();
        expect(screen.getByText(/estimated/i)).toBeInTheDocument();
        // token counts no longer shown
        expect(screen.queryByText("2k")).toBeNull();
    });

    test("USD mode shows em-dash for an unpriced model", () => {
        const unpriced = row("y", 1, 1, {
            model: null,
            outputTokens: 2000,
            cacheCreationTokens: 1000,
            cacheReadTokens: 80_000,
        });
        render(<TaskGrid runId="r1" tasks={[unpriced]} />);
        revealTokens();
        fireEvent.click(screen.getByRole("button", { name: "USD" }));
        // Estimated mode is active, but an unpriced model can't value the
        // buckets — they fall back to em-dash rather than the priced figure.
        expect(screen.getByText(/estimated/i)).toBeInTheDocument();
        expect(screen.queryByText("$0.0300")).toBeNull();
    });
});
