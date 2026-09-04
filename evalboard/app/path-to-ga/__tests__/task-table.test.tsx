import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import type { TagTaskRow } from "@/lib/overview";
import { TagTaskTable } from "../task-table";

// TagTaskTable is a pure props-in/JSX-out component — no router hooks, so unlike
// run-view.render.test.tsx this needs no next/navigation stub.

function row(overrides: Partial<TagTaskRow> = {}): TagTaskRow {
    return {
        taskId: "skill-flow-coded-agent",
        skill: "uipath-maestro-flow",
        appearances: 20,
        matureSkips: 0,
        ungraded: 0,
        executed: 20,
        passRate: 90,
        latestStatus: "SUCCESS",
        latestScore: 1.0,
        latestRunId: "2026-07-31_04-38-51",
        latestMatureSkipped: false,
        ...overrides,
    };
}

function renderTable(
    rows: TagTaskRow[],
    harness: string | null = null,
) {
    return render(
        <TagTaskTable
            rows={rows}
            tag="path-to-ga"
            windowLabel="30d"
            harness={harness}
        />,
    );
}

describe("TagTaskTable", () => {
    test("shows a Mature pill instead of Passed when the latest run skipped the task", () => {
        renderTable([row({ latestMatureSkipped: true })]);
        expect(screen.getByText("Mature")).toBeInTheDocument();
        expect(screen.queryByText("Passed")).not.toBeInTheDocument();
    });

    test("shows Passed and the numeric score for an ordinary executed row", () => {
        renderTable([row({ latestScore: 0.75 })]);
        expect(screen.getByText("Passed")).toBeInTheDocument();
        expect(screen.getByText("0.75")).toBeInTheDocument();
    });

    test("dashes out the latest score on a mature row", () => {
        // 1.0 on a carry-forward row is inherited, not measured — showing it
        // beside a Mature pill would read as a fresh result.
        renderTable([row({ latestMatureSkipped: true, latestScore: 1.0 })]);
        expect(screen.queryByText("1.00")).not.toBeInTheDocument();
        // Exactly one cell dashes — the score. Every other column on the default
        // row is populated, so this pins WHICH cell went un-measured.
        expect(screen.getAllByText("—")).toHaveLength(1);
    });

    test("annotates Appearances with the mature count, and only when non-zero", () => {
        renderTable([row({ appearances: 24, matureSkips: 3 })]);
        expect(screen.getByText("(3 mature)")).toBeInTheDocument();
        // The raw count stays plain beside it (getByText matches an element's own
        // direct text nodes, so this is the cell's "24", not "24 (3 mature)").
        expect(screen.getByText("24")).toBeInTheDocument();
    });

    test("no mature annotation when nothing was skipped", () => {
        renderTable([row({ appearances: 24, matureSkips: 0 })]);
        expect(screen.queryByText(/mature\)/)).not.toBeInTheDocument();
    });

    test("renders an em dash for an unmeasured pass rate", () => {
        // Every appearance was a carry-forward → nothing executed → no rate.
        // Must not read as NaN% or a measured 0%. `latestMatureSkipped` is true
        // by construction here: buildTagTaskRows reads latest* off one of the
        // counted appearances, so matureSkips === appearances forces it — the
        // score dashes too, hence two dashes rather than one.
        renderTable([
            row({
                appearances: 4,
                matureSkips: 4,
                passRate: null,
                latestMatureSkipped: true,
            }),
        ]);
        expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
        expect(screen.queryByText("0%")).not.toBeInTheDocument();
        expect(screen.getAllByText("—")).toHaveLength(2);
    });

    test("the pass-rate tooltip names the row's own executed denominator", () => {
        // Reads `executed` off the row rather than re-deriving it, so the
        // caption and the percentage can't describe different rules. The row
        // below is deliberately inconsistent (executed=5 against 24-3=21) —
        // only a tooltip sourced from the field can report 5.
        renderTable([row({ appearances: 24, matureSkips: 3, executed: 5 })]);
        expect(screen.getByText("90%").closest("td")).toHaveAttribute(
            "title",
            expect.stringContaining("Measured over 5 executed appearances"),
        );
    });

    test("the mature annotation speaks in the window's voice, not one run's", () => {
        renderTable([row({ appearances: 24, matureSkips: 3 })]);
        const title = screen.getByText("(3 mature)").getAttribute("title");
        expect(title).toContain("3 of these 24 appearances");
        // The per-run string would say "this run" on a page that renders none.
        expect(title).not.toContain("this run");
    });

    test("Last appearance shows the date half of the latest run id", () => {
        renderTable([row({ latestRunId: "2026-07-16_04-24-15" })]);
        expect(screen.getByText("2026-07-16")).toBeInTheDocument();
    });

    test("empty rows render the empty state naming the tag and window", () => {
        // Newly reachable: a tag whose every task was de-tagged yields [] where
        // it previously yielded stale rows.
        renderTable([]);
        expect(
            screen.getByText(/No tasks tagged path-to-ga in the last 30d\./),
        ).toBeInTheDocument();
    });

    test("the pooled-across-harnesses note tracks the harness prop", () => {
        const { unmount } = renderTable([row()], null);
        expect(screen.getByText(/pooled across harnesses/)).toBeInTheDocument();
        unmount();

        renderTable([row()], "claude-code");
        expect(
            screen.queryByText(/pooled across harnesses/),
        ).not.toBeInTheDocument();
    });
});
