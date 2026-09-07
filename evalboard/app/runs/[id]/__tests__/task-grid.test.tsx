import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { TaskResultSummary } from "@/lib/runs";
import { TaskGrid } from "../task-grid";

function row(
    taskId: string,
    actualCommands: number | null,
    expectedSeconds: number | null,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        variantId: null,
        replicateIndex: null,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 100,
        totalCostUsd: 0.1,
        actualCommands,
        totalTurns: null,
        expectedTurns: null,
        expectedSeconds,
        hasFinalReply: false,
        inputTokens: null,
        outputTokens: null,
        cacheCreationTokens: null,
        cacheReadTokens: null,
        model: null,
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

function cellFor(taskId: string, index: number): HTMLElement {
    // Scope to the desktop <table>: below md the grid also renders each task as
    // a card (same link/values), so an unscoped query would match twice.
    const table = screen.getByRole("table");
    const link = within(table).getByRole("link", {
        name: new RegExp(taskId, "i"),
    });
    const tr = link.closest("tr")!;
    const cells = within(tr).getAllByRole("cell");
    return cells[index]!;
}

// Layout: Task, Status, Score, Duration, vs Exp, Cost, Turns, then the tokens
const durationCellFor = (taskId: string) => cellFor(taskId, 3);
const vsExpCellFor = (taskId: string) => cellFor(taskId, 4);
const turnsCellFor = (taskId: string) => cellFor(taskId, 6);

describe("TaskGrid — mature rows", () => {
    test("opens a popover linking to the run where it last executed", () => {
        render(
            <TaskGrid sourceId="skills"
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
            <TaskGrid sourceId="skills"
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

describe("TaskGrid — vs Expected column", () => {
    // row() fixes durationSeconds at 100s, so expectedSeconds sets the ratio.
    const ratioRows = [
        row("over", 3, 40), // ratio 2.5 → red (> 2)
        row("mid", 3, 56), // ratio 1.79 → 1.8× → yellow (1.5 < r ≤ 2)
        row("under", 3, 250), // ratio 0.4 → green (≤ 1.5)
        row("unscored", 3, null), // no line yet → em dash, no tint
    ];

    test("prints the ratio so it is readable without hovering", () => {
        render(
            <TaskGrid sourceId="skills" runId="r1" tasks={ratioRows} />,
        );
        expect(vsExpCellFor("over")).toHaveTextContent("2.5×");
        expect(vsExpCellFor("mid")).toHaveTextContent("1.8×");
        expect(vsExpCellFor("under")).toHaveTextContent("0.4×");
        expect(vsExpCellFor("unscored")).toHaveTextContent("—");
    });

    test("colorizes the ratio per bucket (no background)", () => {
        render(
            <TaskGrid sourceId="skills" runId="r1" tasks={ratioRows} />,
        );

        const overCell = vsExpCellFor("over");
        expect(overCell.className).toContain("text-rose-700");
        expect(overCell.className).not.toContain("bg-");
        expect(overCell).toHaveAttribute("title", "expected time: 0m40s");

        expect(vsExpCellFor("mid").className).toContain("text-amber-700");
        expect(vsExpCellFor("under").className).toContain("text-emerald-700");

        const unscoredCell = vsExpCellFor("unscored");
        expect(unscoredCell.className).toContain("text-gray-900");
        expect(unscoredCell.className).not.toMatch(
            /text-(rose|amber|emerald)-/,
        );
        expect(unscoredCell).toHaveAttribute(
            "title",
            "no expected time yet (needs a passing run on this harness)",
        );
    });

    test("Duration stays untinted, so length is never mistaken for slowness", () => {
        render(
            <TaskGrid sourceId="skills" runId="r1" tasks={ratioRows} />,
        );
        for (const id of ["over", "mid", "under", "unscored"]) {
            expect(durationCellFor(id).className).not.toMatch(
                /text-(rose|amber|emerald)-/,
            );
        }
    });

    test("sorts by ratio, which is a different order than by duration", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[
                    // Long but on pace; short but far past its line.
                    row("long", 3, 400, { durationSeconds: 600 }), // 1.50×
                    row("short", 3, 10, { durationSeconds: 30 }), // 3.00×
                ]}
            />,
        );
        const order = () =>
            within(screen.getByRole("table"))
                .getAllByRole("row")
                .slice(1)
                .map((tr) => within(tr).getAllByRole("cell")[0].textContent);

        fireEvent.click(screen.getByRole("button", { name: /^Duration$/ }));
        expect(order()[0]).toMatch(/long/i);

        fireEvent.click(screen.getByRole("button", { name: /^vs Expected$/ }));
        expect(order()[0]).toMatch(/short/i);
    });
});

describe("TaskGrid — Turns column", () => {
    // The turn budget still tints its own column, beside the wall-clock ratio.
    // Both signals are shown while the derived expected-time line is watched.
    test("colorizes the digits per ratio bucket (no background)", () => {
        render(
            <TaskGrid sourceId="skills"
                runId="r1"
                tasks={[
                    row("over", 10, null, { expectedTurns: 5 }), // ratio 2.0 → red (> 1.5)
                    row("mid", 7, null, { expectedTurns: 5 }), // ratio 1.4 → yellow (1.25 < r ≤ 1.5)
                    row("under", 4, null, { expectedTurns: 10 }), // ratio 0.4 → green (≤ 1.25)
                    row("notarget", 7, null), // black-ish default
                ]}
            />,
        );

        const overCell = turnsCellFor("over");
        expect(overCell).toHaveTextContent("10");
        expect(overCell.className).toContain("text-rose-700");
        expect(overCell.className).not.toContain("bg-");
        expect(overCell).toHaveAttribute("title", "expected_turns target: 5");

        expect(turnsCellFor("mid").className).toContain("text-amber-700");
        expect(turnsCellFor("under").className).toContain("text-emerald-700");

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
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[row("legacy", null, null)]} />);
        const cell = turnsCellFor("legacy");
        expect(cell).toHaveTextContent("—");
        expect(cell.className).toContain("text-gray-900");
    });

    test("token columns are collapsed by default, revealed by the toggle", () => {
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[row("x", 1, 1)]} />);
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
            "vs Expected",
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
            "vs Expected",
            "Cost",
            "Turns",
            "In",
            "Cache R",
            "Cache W",
            "Out",
        ]);
    });
});

describe("TaskGrid — column tooltips", () => {
    test("definitions ride on the header title, not an ⓘ popover", () => {
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[row("x", 1, 1)]} />);
        revealTokens();
        const header = (label: string) =>
            screen
                .getAllByRole("columnheader")
                .find((h) => h.textContent?.trim().startsWith(label))!;

        expect(header("Cache R")).toHaveAttribute(
            "title",
            expect.stringContaining("Cache-read tokens"),
        );
        expect(header("vs Expected")).toHaveAttribute(
            "title",
            expect.stringContaining("Duration ÷"),
        );
        expect(header("Turns")).toHaveAttribute(
            "title",
            expect.stringContaining("expected_turns"),
        );
        // No ⓘ buttons anywhere: each header carries its sort toggle and nothing else.
        for (const h of screen.getAllByRole("columnheader")) {
            expect(within(h).getAllByRole("button")).toHaveLength(1);
        }
        expect(screen.queryByRole("tooltip")).toBeNull();
    });
});

describe("TaskGrid — dataset-expanded task links", () => {
    test("link href uses the full slash-separated task ID", () => {
        render(
            <TaskGrid sourceId="skills"
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
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[priced]} />);
        revealTokens();
        // Scope value lookups to the table — the mobile card duplicates them.
        const table = screen.getByRole("table");
        expect(within(table).getByText("2k")).toBeInTheDocument();
        expect(screen.queryByText(/estimated/i)).toBeNull();
    });

    test("USD mode prices each bucket and shows an 'estimated' badge", () => {
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[priced]} />);
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
        render(<TaskGrid sourceId="skills" runId="r1" tasks={[unpriced]} />);
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
            <TaskGrid sourceId="skills"
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
            <TaskGrid sourceId="skills"
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
            <TaskGrid sourceId="skills"
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
            <TaskGrid sourceId="skills"
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

describe("TaskGrid — variants", () => {
    // A run with no variants must render exactly as it always has: no extra
    // column, no extra chip, no ?v= on any link.
    test("an ordinary run gains no variant column and no ?v= link", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[row("alpha", 3, 5), row("beta", 3, 5)]}
            />,
        );
        const table = screen.getByRole("table");
        expect(
            within(table).queryByRole("columnheader", { name: /variant/i }),
        ).toBeNull();
        const link = within(table).getByRole("link", { name: /alpha/i });
        expect(link.getAttribute("href")).not.toContain("v=");
    });

    test("a two-arm run shows the column and one row per arm", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[
                    row("alpha", 3, 5, {
                        variantId: "live-v1",
                        status: "SUCCESS",
                    }),
                    row("alpha", 3, 5, {
                        variantId: "preview-v2",
                        status: "FAILURE",
                    }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        expect(
            within(table).getByRole("columnheader", { name: /variant/i }),
        ).toBeTruthy();
        // Both arms survive: the collapse groups replicates, never arms.
        expect(within(table).getAllByRole("link", { name: /alpha/i })).toHaveLength(
            2,
        );
        expect(within(table).getByText("live-v1")).toBeTruthy();
        expect(within(table).getByText("preview-v2")).toBeTruthy();
    });

    // Without ?v= both rows would open the same transcript, which is the exact
    // failure this change exists to fix.
    test("each arm's row links to its own arm", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[
                    row("alpha", 3, 5, { variantId: "live-v1" }),
                    row("alpha", 3, 5, { variantId: "preview-v2" }),
                ]}
            />,
        );
        const table = screen.getByRole("table");
        const hrefs = within(table)
            .getAllByRole("link", { name: /alpha/i })
            .map((a) => a.getAttribute("href") ?? "");
        expect(hrefs.some((h) => h.includes("v=live-v1"))).toBe(true);
        expect(hrefs.some((h) => h.includes("v=preview-v2"))).toBe(true);
    });
});

describe("TaskGrid — default ordering keeps a task's arms together", () => {
    // The case that motivated the rule: one task's arms DISAGREE. Ranking rows
    // independently sends the failing arm to the top and the passing arm to the
    // bottom, which is precisely the comparison the run was made to show.
    test("a task whose arms disagree still renders its two rows adjacent", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[
                    row("alpha", null, null, { variantId: "A", status: "FAILURE" }),
                    row("alpha", null, null, { variantId: "B", status: "SUCCESS" }),
                    row("beta", null, null, { variantId: "A", status: "SUCCESS" }),
                    row("beta", null, null, { variantId: "B", status: "SUCCESS" }),
                ]}
            />,
        );
        const order = screen
            .getAllByRole("row")
            .slice(1)
            .map((tr) => tr.textContent ?? "");

        // alpha's arms are rows 0 and 1: the failing task sorts first, and its
        // passing arm comes with it rather than being ranked to the bottom.
        expect(order[0]).toMatch(/alpha/i);
        expect(order[1]).toMatch(/alpha/i);
        expect(order[2]).toMatch(/beta/i);
        expect(order[3]).toMatch(/beta/i);
    });

    test("a run without variants keeps failures-first, then task id", () => {
        render(
            <TaskGrid
                sourceId="skills"
                runId="r1"
                tasks={[
                    row("aaa", null, null, { status: "SUCCESS" }),
                    row("mmm", null, null, { status: "FAILURE" }),
                    row("zzz", null, null, { status: "SUCCESS" }),
                ]}
            />,
        );
        const order = screen
            .getAllByRole("row")
            .slice(1)
            .map((tr) => tr.textContent ?? "");
        expect(order[0]).toMatch(/mmm/i);
        expect(order[1]).toMatch(/aaa/i);
        expect(order[2]).toMatch(/zzz/i);
    });
});
