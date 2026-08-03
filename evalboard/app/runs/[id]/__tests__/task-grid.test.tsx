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
        replicateIndex: null,
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
        variant: null,
        tags: [],
        skill: null,
        matureSkipped: false,
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

describe("TaskGrid — mature rows", () => {
    test("opens a popover linking to the run where it last executed", () => {
        render(
            <TaskGrid
                runId="r2"
                tasks={[
                    row("rantask", 3, 5),
                    row("skippedtask", 0, 5, { matureSkipped: true }),
                ]}
                matureSourceRuns={{ skippedtask: "r1" }}
            />,
        );

        const table = screen.getByRole("table");
        // The mature id is a popover trigger, not a direct link — no cross-run
        // navigation happens until the user opens it and clicks through.
        const trigger = within(table).getByRole("button", {
            name: /skippedtask/i,
        });
        // ↗ marks the cross-run jump; tooltip names the source run.
        expect(trigger.textContent).toContain("↗");
        expect(trigger).toHaveAttribute(
            "title",
            expect.stringContaining("Last ran in run r1"),
        );
        // Closed by default — the source link only exists once opened. (The
        // card portals to <body>, so it's queried document-wide, not in-table.)
        expect(
            screen.queryByRole("link", { name: /open that execution/i }),
        ).toBeNull();

        fireEvent.click(trigger);
        const link = screen.getByRole("link", {
            name: /open that execution/i,
        });
        expect(link).toHaveAttribute("href", "/runs/r1/skippedtask");

        // The normal task keeps its in-run detail link.
        expect(
            within(table).getByRole("link", { name: /rantask/i }),
        ).toHaveAttribute("href", "/runs/r2/rantask");
        // The Mature status badge still renders.
        expect(screen.getAllByText("Mature").length).toBeGreaterThanOrEqual(1);
    });

    test("falls back to a non-clickable id when no source run is known", () => {
        render(
            <TaskGrid
                runId="r2"
                tasks={[row("skippedtask", 0, 5, { matureSkipped: true })]}
                matureSourceRuns={{}}
            />,
        );

        const table = screen.getByRole("table");
        expect(
            within(table).queryByRole("link", { name: /skippedtask/i }),
        ).toBeNull();
        const badges = screen.getAllByText("Mature");
        expect(badges[0]).toHaveAttribute(
            "title",
            expect.stringContaining("skipped this run"),
        );
    });
});

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

describe("TaskGrid — replicates", () => {
    test("collapses replicates to one row with a k/N ✓ badge linking to the task page", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("reptask", 1, 5, { replicateIndex: 0 }),
                    row("reptask", 1, 5, { replicateIndex: 1 }),
                    row("reptask", 1, 5, { replicateIndex: 2 }),
                    row("solo", 1, 5, { replicateIndex: 0 }),
                ]}
            />,
        );
        const table = screen.getByRole("table");

        // The 3 replicates collapse to a SINGLE reptask row (the original bug was
        // 3 indistinguishable rows); solo stays its own row.
        const reptaskLinks = within(table).getAllByRole("link", {
            name: /reptask/i,
        });
        expect(reptaskLinks).toHaveLength(1);
        // The collapsed row links to its representative replicate (?r=0 here,
        // since all passed → lowest index wins); the run selector switches runs.
        expect(reptaskLinks[0]).toHaveAttribute("href", "/runs/r1/reptask?r=0");
        // k/N ✓ badge: all 3 replicates passed → "3 of 3", GREEN. solo (single
        // run) shows no badge.
        const badge = within(table).getByTitle(/3 of 3 replicates passed/i);
        expect(badge.textContent?.replace(/\s/g, "")).toBe("3/3✓");
        expect(badge.className).toContain("text-green-700");
    });

    test("k/N ✓ badge counts only the passing replicates and is amber for a partial pass", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("mixed", 1, 5, { replicateIndex: 0, status: "SUCCESS" }),
                    row("mixed", 1, 5, { replicateIndex: 1, status: "FAILURE" }),
                    row("mixed", 1, 5, { replicateIndex: 2, status: "ERROR" }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        // 1 of 3 replicates passed → shows the pass count, AMBER (partial).
        const badge = within(table).getByTitle(/1 of 3 replicates passed/i);
        expect(badge.textContent?.replace(/\s/g, "")).toBe("1/3✓");
        expect(badge.className).toContain("text-amber-700");
    });

    test("k/N ✓ badge is red when no replicate passed", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("none", 1, 5, { replicateIndex: 0, status: "FAILURE" }),
                    row("none", 1, 5, { replicateIndex: 1, status: "ERROR" }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        const badge = within(table).getByTitle(/0 of 2 replicates passed/i);
        expect(badge.textContent?.replace(/\s/g, "")).toBe("0/2✓");
        expect(badge.className).toContain("text-red-700");
    });

    test("collapsed row picks a PASSING replicate: Passed pill and link to that run", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    // Lowest index failed; replicate 1 passed → 1 is the rep.
                    row("t", 1, 5, { replicateIndex: 0, status: "FAILURE" }),
                    row("t", 1, 5, { replicateIndex: 1, status: "SUCCESS" }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        const link = within(table).getByRole("link", { name: /^t/i });
        const tr = link.closest("tr")!;
        // Status pill reflects the task outcome (any-pass → Passed).
        expect(within(tr).getByText("Passed")).toBeInTheDocument();
        expect(within(tr).queryByText("Failed")).toBeNull();
        // The row links to the PASSING replicate (?r=1) — not replicate 0's
        // failure — so status, metrics and destination all describe run 1.
        expect(link).toHaveAttribute("href", "/runs/r1/t?r=1");
        // Badge still shows the true ratio.
        expect(
            within(tr).getByTitle(/1 of 2 replicates passed/i).textContent?.replace(/\s/g, ""),
        ).toBe("1/2✓");
    });
});

describe("TaskGrid — multi-model (A/B) runs", () => {
    test("shows a Model column and one row per model when models differ", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("t", 3, 5, {
                        variant: "kimi-k3",
                        model: "moonshotai/kimi-k3",
                    }),
                    row("t", 3, 5, {
                        variant: "glm-5-2",
                        model: "z-ai/glm-5.2",
                    }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        // The Model column header appears only for multi-model runs.
        expect(
            within(table).getByRole("columnheader", { name: /Model/i }),
        ).toBeInTheDocument();
        // Both models are rendered — the two variants stay distinct rows, not
        // collapsed into one (the multi-model "no info displayed" bug).
        expect(within(table).getByText("moonshotai/kimi-k3")).toBeInTheDocument();
        expect(within(table).getByText("z-ai/glm-5.2")).toBeInTheDocument();
    });

    test("each model's row links to its OWN variant via ?v=", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("t", 3, 5, {
                        variant: "kimi-k3",
                        model: "moonshotai/kimi-k3",
                    }),
                    row("t", 3, 5, {
                        variant: "glm-5-2",
                        model: "z-ai/glm-5.2",
                    }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        const links = within(table).getAllByRole("link", { name: /^t/i });
        const hrefs = links.map((l) => l.getAttribute("href")).sort();
        expect(hrefs).toEqual(["/runs/r1/t?v=glm-5-2", "/runs/r1/t?v=kimi-k3"]);
    });

    test("hides the arm column for a single-arm run", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("a", 3, 5, { variant: "default", model: "claude-sonnet-4-6" }),
                    row("b", 3, 5, { variant: "default", model: "claude-sonnet-4-6" }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        expect(
            within(table).queryByRole("columnheader", { name: /Model/i }),
        ).toBeNull();
        expect(
            within(table).queryByRole("columnheader", { name: /Variant/i }),
        ).toBeNull();
    });

    test("same-model A/B: shows a Variant column labeling each arm", () => {
        // Skill on/off (or terse/detailed): same model, different variant. Rows
        // split on variant, so they must be distinguishable — gating on distinct
        // MODEL would render two identical unlabeled rows (the reported gap).
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("t", 3, 5, { variant: "bare", model: "claude-sonnet-4-6" }),
                    row("t", 3, 5, {
                        variant: "with-skill",
                        model: "claude-sonnet-4-6",
                    }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        // Header reads "Variant" (models don't differ), not "Model".
        expect(
            within(table).getByRole("columnheader", { name: /Variant/i }),
        ).toBeInTheDocument();
        // Both arms are labeled and distinct.
        expect(within(table).getByText("bare")).toBeInTheDocument();
        expect(within(table).getByText("with-skill")).toBeInTheDocument();
        // Each arm links to its own ?v=.
        const hrefs = within(table)
            .getAllByRole("link", { name: /^t/i })
            .map((l) => l.getAttribute("href"))
            .sort();
        expect(hrefs).toEqual(["/runs/r1/t?v=bare", "/runs/r1/t?v=with-skill"]);
    });
});
