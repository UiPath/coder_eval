import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import type { PerRun } from "@/lib/overview";
import type { RunOverview, RunOverviewTask } from "@/lib/runs";
import { buildWatchlist } from "@/lib/watchlist";
import { WatchlistView } from "../watchlist-view";

function task(o: Partial<RunOverviewTask>): RunOverviewTask {
    return {
        taskId: "t1",
        status: "SUCCESS",
        tags: [],
        skill: null,
        totalCostUsd: null,
        durationSeconds: null,
        weightedScore: null,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        visibleTurns: null,
        hasFinalReply: false,
        ...o,
    };
}
function run(id: string, tasks: RunOverviewTask[]): PerRun {
    const overview: RunOverview = {
        id,
        tasks,
        totalCostUsd: null,
        taskDurationSeconds: null,
        componentShas: [],
    };
    return { id, overview, reviewTagCounts: {}, reviewTagsByTask: {} };
}

describe("WatchlistView", () => {
    test("renders hero + panels with real aggregated data", () => {
        const data = buildWatchlist([
            run("2026-01-02", [
                task({ taskId: "a", skill: "alpha", status: "FAILURE" }),
                task({ taskId: "b", skill: "beta", status: "SUCCESS" }),
            ]),
            run("2026-01-01", [
                task({ taskId: "a", skill: "alpha", status: "FAILURE" }),
                task({ taskId: "b", skill: "beta", status: "FAILURE" }),
            ]),
        ]);
        render(<WatchlistView data={data} />);

        expect(screen.getByText("Watchlist")).toBeInTheDocument();
        expect(screen.getByText("Needs Attention")).toBeInTheDocument();
        expect(screen.getByText("🔴 Never passed")).toBeInTheDocument();
        expect(screen.getByText("📉 Skills leaderboard")).toBeInTheDocument();
        expect(screen.getByText("🎢 Yee-Yaw — least stable")).toBeInTheDocument();
        // "alpha" failed both runs -> appears in the hero and leaderboard.
        expect(screen.getAllByText("alpha").length).toBeGreaterThan(0);
    });

    test("expands a list with tied offenders beyond the cap", () => {
        // 12 skills all failing once in one run -> 12 leaderboard rows tied at
        // 0%. The cap is 10, so the 11th/12th must be reachable via the
        // expander, with a tie note, rather than silently truncated.
        const tasks = Array.from({ length: 12 }, (_, i) =>
            task({
                taskId: `t${String(i).padStart(2, "0")}`,
                skill: `s${String(i).padStart(2, "0")}`,
                status: "FAILURE",
            }),
        );
        render(
            <WatchlistView data={buildWatchlist([run("2026-01-01", tasks)])} />,
        );
        // The expander summary appears with the full count + tie callout.
        expect(screen.getByText(/Show all 12 \(2 more tied\)/)).toBeInTheDocument();
        // Beyond-cap rows are in the DOM (inside <details>), not dropped.
        expect(screen.getAllByText("s11").length).toBeGreaterThan(0);
    });

    test("empty window renders friendly empty states without throwing", () => {
        render(<WatchlistView data={buildWatchlist([])} />);
        expect(screen.getByText("Nothing needs attention 🎉")).toBeInTheDocument();
        expect(screen.getByText("last 0 runs")).toBeInTheDocument();
    });
});
