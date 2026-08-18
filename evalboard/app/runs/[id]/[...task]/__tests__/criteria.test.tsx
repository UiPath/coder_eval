import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { CriterionResult } from "@/lib/runs";
import { CriteriaSection } from "../_sections";

function criterion(
    overrides: Partial<CriterionResult> = {},
): CriterionResult {
    return {
        criterionType: "file_exists",
        description: "artifact exists",
        score: 1,
        details: null,
        error: null,
        evaluationStatus: "evaluated",
        passThreshold: 0.9,
        gating: true,
        ...overrides,
    };
}

describe("CriteriaSection", () => {
    test("renders unavailable post-failure checks without calling them failures", () => {
        render(
            <CriteriaSection
                title="Post-failure artifact evidence"
                diagnostic
                criteria={[
                    criterion(),
                    criterion({
                        criterionType: "run_command",
                        description: "validator runs",
                        score: 0,
                        evaluationStatus: "not_evaluated",
                    }),
                ]}
            />,
        );

        expect(
            screen.getByText("Post-failure artifact evidence (2)"),
        ).toBeInTheDocument();
        expect(screen.getByText("NOT EVALUATED")).toBeInTheDocument();
        expect(screen.getByText("no score")).toBeInTheDocument();
        expect(
            screen.getByText(/does not affect status, score, or pass\/fail gating/),
        ).toBeInTheDocument();
    });
});
