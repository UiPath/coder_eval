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
