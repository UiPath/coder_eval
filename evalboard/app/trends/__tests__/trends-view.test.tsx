import { describe, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { TaskTrend } from "@/lib/trends";
import { KNOWN_HARNESSES } from "@/app/_components/harness-badge";

// The view imports the server action for the expandable history rows; stub it
// so rendering doesn't pull the blob-backed loader into a jsdom test.
vi.mock("../actions", () => ({
    fetchTaskHistoryAction: vi.fn(async () => []),
}));

// The header's HarnessSelector reads router/params hooks; stub them so the view
// renders in jsdom without a router provider.
vi.mock("next/navigation", () => ({
    useRouter: () => ({ replace: vi.fn() }),
    usePathname: () => "/trends",
    useSearchParams: () => new URLSearchParams(),
}));

const { TrendsView } = await import("../trends-view");

function trend(overrides: Partial<TaskTrend>): TaskTrend {
    return {
        taskId: "t1",
        skill: null,
        tags: [],
        totalRuns: 1,
        successRuns: 1,
        passRate: 1,
        avgDurationSeconds: null,
        avgCostUsd: null,
        avgActualCommands: null,
        avgTotalTurns: null,
        recentStatuses: [],
        dominantFailureTags: [],
        ...overrides,
    };
}

function renderView(tasks: TaskTrend[], runIds: string[]) {
    return render(
        <TrendsView
            tasks={tasks}
            runIds={runIds}
            q={null}
            activeTag={null}
            activeHarness="claude-code"
            harnesses={KNOWN_HARNESSES}
            skills={[]}
            taskTags={[]}
            reviewTags={[]}
            provenance={null}
        />,
    );
}

describe("TrendsView — status strip run-axis alignment", () => {
    test("one slot per axis run; hollow stub where the task is absent", () => {
        renderView(
            [
                trend({
                    taskId: "t1",
                    totalRuns: 2,
                    recentStatuses: [
                        { runId: "r2", status: "SUCCESS" },
                        { runId: "r1", status: null },
                    ],
                }),
            ],
            // Newest-first axis; the task is absent from the newest run r3.
            ["r3", "r2", "r1"],
        );
        const gap = screen.getByTitle("r3 · not in run");
        const success = screen.getByTitle("r2 · SUCCESS");
        const unknown = screen.getByTitle("r1 · unknown");
        // The absent-run slot is the hollow stub; present runs are full bars
        // (including a present-but-null status, which renders as "unknown").
        // `spark-bar` is the full-height bar geometry (app/globals.css); the
        // stub keeps its own short height, so the token is the discriminator.
        expect(gap.className).toContain("border-gray-300");
        expect(gap.className).not.toContain("spark-bar");
        expect(success.className).toContain("spark-bar");
        expect(unknown.className).toContain("spark-bar");
        // Strip renders oldest → newest, left to right.
        const titles = [...gap.parentElement!.children].map((c) =>
            c.getAttribute("title"),
        );
        expect(titles).toEqual([
            "r1 · unknown",
            "r2 · SUCCESS",
            "r3 · not in run",
        ]);
    });

    test("a task missing the newest runs shows trailing gaps, not flush-right", () => {
        // The regression this PR fixes: a renamed/retired task whose data
        // ends mid-window must not look identical to one that ran in the
        // newest run.
        renderView(
            [
                trend({
                    taskId: "old",
                    recentStatuses: [{ runId: "r1", status: "SUCCESS" }],
                }),
            ],
            ["r3", "r2", "r1"],
        );
        const strip = screen.getByTitle("r1 · SUCCESS").parentElement!;
        const titles = [...strip.children].map((c) => c.getAttribute("title"));
        expect(titles).toEqual([
            "r1 · SUCCESS",
            "r2 · not in run",
            "r3 · not in run",
        ]);
    });
});

describe("TrendsView — maturity indications", () => {
    test("summary shows x/y (pct) mature, row shows a bare badge", () => {
        renderView(
            [
                trend({
                    taskId: "matured",
                    totalRuns: 3,
                    matureSkips: 2,
                    recentStatuses: [
                        { runId: "r3", status: "SUCCESS", matureSkipped: true },
                        { runId: "r2", status: "SUCCESS", matureSkipped: true },
                        { runId: "r1", status: "SUCCESS" },
                    ],
                }),
                trend({ taskId: "fresh", matureSkips: 0 }),
            ],
            ["r3", "r2", "r1"],
        );
        // Header summary: 1 of 2 tasks in view are mature.
        expect(screen.getByText("1/2 (50%) mature")).toBeTruthy();
        // Per-row badge is a bare "Mature" — no skip count.
        expect(screen.getByText("Mature")).toBeTruthy();
    });

    test("mature-skipped run slots render as hollow-green, distinct from real passes", () => {
        renderView(
            [
                trend({
                    taskId: "matured",
                    totalRuns: 2,
                    matureSkips: 1,
                    recentStatuses: [
                        { runId: "r2", status: "SUCCESS", matureSkipped: true },
                        { runId: "r1", status: "SUCCESS" },
                    ],
                }),
            ],
            ["r2", "r1"],
        );
        const skipped = screen.getByTitle(
            "r2 · mature (skipped, carried forward)",
        );
        const realPass = screen.getByTitle("r1 · SUCCESS");
        expect(skipped.className).toContain("bg-green-100");
        expect(skipped.className).toContain("border-green-400");
        expect(realPass.className).toContain("bg-green-500");
    });

    test("no maturity summary when nothing is mature", () => {
        renderView([trend({ taskId: "t1", matureSkips: 0 })], ["r1"]);
        expect(screen.queryByText(/mature/)).toBeNull();
    });
});
