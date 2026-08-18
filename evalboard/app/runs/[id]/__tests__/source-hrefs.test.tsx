import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ActivationScore, TaskResultSummary } from "@/lib/runs";
import { ActivationCard } from "../activation-card";
import { TaskGrid } from "../task-grid";

function row(
    taskId: string,
    extra: Partial<TaskResultSummary> = {},
): TaskResultSummary {
    return {
        taskId,
        replicateIndex: null,
        status: "SUCCESS",
        weightedScore: 1.0,
        durationSeconds: 1.0,
        totalCostUsd: 0.1,
        actualCommands: null,
        totalTurns: null,
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

const ACTIVATION: ActivationScore = {
    score: 0.5,
    denominator: 4,
    nSkillsSampled: 2,
    minPrompts: 3,
    nCases: 10,
    perSkill: [],
};

// Scope to the desktop <table>: TaskGrid also renders each task as a mobile
// card with the same link, so an unscoped query matches twice.
function tableLink(name: RegExp): HTMLElement {
    return within(screen.getByRole("table")).getByRole("link", { name });
}

// Run ids are only unique within a container, so a link that loses ?src lands on
// a DIFFERENT run's data instead of failing. The default source is left off so
// every URL that predates the Scribe tab stays byte-identical.
describe("source in run-page hrefs", () => {
    test("task links carry ?src for a non-default source", () => {
        render(
            <TaskGrid runId="r1" tasks={[row("alpha")]} sourceId="scribe" />,
        );
        expect(tableLink(/alpha/i)).toHaveAttribute(
            "href",
            "/runs/r1/alpha?src=scribe",
        );
    });

    test("task links omit ?src for the default source", () => {
        // No "and when unset" case any more: `sourceId` is a REQUIRED prop, so
        // omitting it is a compile error rather than a silent fallback to the
        // default source. That's the point — the fallback was invisible both at
        // build time and at runtime.
        render(<TaskGrid runId="r1" tasks={[row("alpha")]} sourceId="skills" />);
        expect(tableLink(/alpha/i)).toHaveAttribute("href", "/runs/r1/alpha");
    });

    test("a replicate link keeps ?r and gains ?src", () => {
        render(
            <TaskGrid
                runId="r1"
                tasks={[
                    row("alpha", { replicateIndex: 0 }),
                    row("alpha", { replicateIndex: 1 }),
                ]}
                sourceId="scribe"
            />,
        );
        expect(tableLink(/alpha/i)).toHaveAttribute(
            "href",
            "/runs/r1/alpha?r=0&src=scribe",
        );
    });

    test("a mature row's earlier-execution link stays in the same source", () => {
        render(
            <TaskGrid
                runId="r2"
                tasks={[row("alpha", { matureSkipped: true })]}
                matureSourceRuns={{ alpha: "r1" }}
                sourceId="scribe"
            />,
        );
        fireEvent.click(
            within(screen.getByRole("table")).getByRole("button", {
                name: /alpha/i,
            }),
        );
        expect(
            screen.getByRole("link", { name: /open that execution/i }),
        ).toHaveAttribute("href", "/runs/r1/alpha?src=scribe");
    });

    test("the activation card links into the same source", () => {
        const { unmount } = render(
            <ActivationCard
                runId="r1"
                activation={ACTIVATION}
                sourceId="scribe"
            />,
        );
        expect(screen.getByRole("link")).toHaveAttribute(
            "href",
            "/runs/r1/activation?src=scribe",
        );
        unmount();

        render(
            <ActivationCard
                runId="r1"
                activation={ACTIVATION}
                sourceId="skills"
            />,
        );
        expect(screen.getByRole("link")).toHaveAttribute(
            "href",
            "/runs/r1/activation",
        );
    });
});
