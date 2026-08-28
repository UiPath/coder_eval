import { describe, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { TaskResultSummary } from "@/lib/runs";

// RunView reads the URL via next/navigation hooks; stub them so it renders in
// jsdom. The filter state we don't exercise here just resolves to "no filter".
vi.mock("next/navigation", () => ({
    useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
    usePathname: () => "/runs/r1",
    useSearchParams: () => new URLSearchParams(),
}));

const { RunView } = await import("../run-view");

function row(
    taskId: string,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        variantId: null,
        replicateIndex: null,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands: null,
        totalTurns: null,
        expectedTurns: null,
        expectedSeconds: null,
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

describe("RunView — Pass-rate / Failed tiles for repeated runs", () => {
    test("headline is per-task with a per-replicate sub-line; Failed matches", () => {
        // task A: 1/2 replicates pass → task passes. task B: 0/2 → task fails.
        // Per-task: 1/2 passed, 1 failed. Per-replicate: 1/4 passed, 3 failed.
        render(
            <RunView sourceId="skills"
                runId="r1"
                tasks={[
                    row("A", { replicateIndex: 0, status: "SUCCESS" }),
                    row("A", { replicateIndex: 1, status: "FAILURE" }),
                    row("B", { replicateIndex: 0, status: "FAILURE" }),
                    row("B", { replicateIndex: 1, status: "FAILURE" }),
                ]}
            />,
        );

        // Headline: per-task rate + "/ N tasks", with per-replicate as sub-line.
        expect(screen.getByText(/1\s*\/\s*2\s*tasks/)).toBeInTheDocument();
        expect(
            screen.getByText(/1\s*\/\s*4\s*replicate runs/),
        ).toBeInTheDocument();
        // Failed tile is per-task (1), with the per-replicate breakdown as sub.
        expect(
            screen.getByText(/3\s*of\s*4\s*replicate runs/),
        ).toBeInTheDocument();
    });

    test("single-shot run keeps the plain per-replicate rate (no 'tasks' suffix)", () => {
        render(
            <RunView sourceId="skills"
                runId="r1"
                tasks={[
                    row("A", { status: "SUCCESS" }),
                    row("B", { status: "FAILURE" }),
                ]}
            />,
        );
        // No repeats → headline shows "1 / 2" without the " tasks" suffix and no
        // "replicate runs" sub-line.
        expect(screen.getByText(/1\s*\/\s*2$/)).toBeInTheDocument();
        expect(screen.queryByText(/replicate runs/)).toBeNull();
    });
});

describe("RunView — multi-variant runs replace the pooled pass rate", () => {
    // Arm A passes everything, arm B fails everything. The pooled rate would be
    // 50%, which describes neither configuration; the whole point of the tile
    // change is that this number no longer appears anywhere on the page.
    const AB = [
        row("X", { variantId: "A", status: "SUCCESS", totalCostUsd: 0.1, durationSeconds: 1 }),
        row("Y", { variantId: "A", status: "SUCCESS", totalCostUsd: 0.1, durationSeconds: 1 }),
        row("X", { variantId: "B", status: "FAILURE", totalCostUsd: 0.3, durationSeconds: 5 }),
        row("Y", { variantId: "B", status: "FAILURE", totalCostUsd: 0.3, durationSeconds: 5 }),
    ];

    test("each arm gets its own rate and the blended rate is gone", () => {
        render(<RunView sourceId="skills" runId="r1" tasks={AB} />);

        expect(screen.getByText("100%")).toBeInTheDocument();
        expect(screen.getByText("0%")).toBeInTheDocument();
        // The blended 50% must not be rendered at all.
        expect(screen.queryByText("50%")).toBeNull();
        expect(screen.getByText(/2 arms · spread 100 pts/)).toBeInTheDocument();
    });

    test("spend and elapsed time keep their pooled total, split by arm on the sub-line", () => {
        render(<RunView sourceId="skills" runId="r1" tasks={AB} />);

        // Pooled totals stay: a run's cost is real however many arms produced it.
        expect(screen.getByText("$0.80")).toBeInTheDocument();
        expect(screen.getByText("12s")).toBeInTheDocument();
        // ...with the per-arm split replacing p50/p90, which would describe a
        // pooled population that does not exist.
        expect(screen.getByText("A $0.20 · B $0.60")).toBeInTheDocument();
        expect(screen.getByText("A 2s · B 10s")).toBeInTheDocument();
        expect(screen.queryByText(/p50/)).toBeNull();
    });

    test("a single-arm run is untouched: pooled rate and p50/p90 as before", () => {
        render(
            <RunView
                sourceId="skills"
                runId="r1"
                tasks={[
                    row("X", { variantId: "only", status: "SUCCESS" }),
                    row("Y", { variantId: "only", status: "FAILURE" }),
                ]}
            />,
        );
        expect(screen.getByText("50%")).toBeInTheDocument();
        expect(screen.queryByText(/arms · spread/)).toBeNull();
        // Both the cost and time tiles carry one, hence getAll.
        expect(screen.getAllByText(/p50/).length).toBe(2);
    });
});
