import { describe, expect, test } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AnalysisPanel } from "../analysis-panel";
import { planAnalysis } from "@/lib/doctor";

const MD = "# Analysis\n\n## TL;DR\nTop.\n\n## Findings\nBottom.\n";

describe("AI summary panel without the doctor block", () => {
    test("the plan hands the page the whole file and no block", () => {
        expect(planAnalysis(MD, null)).toEqual({ summary: MD, rest: null, doctor: null });
    });

    test("the panel renders the whole analysis, Findings included, as before", () => {
        render(<AnalysisPanel markdown={MD} />);
        expect(screen.queryByText("Bottom.")).toBeNull();
        fireEvent.click(screen.getByRole("button", { name: /AI summary/ }));
        expect(screen.getByText("Top.")).toBeInTheDocument();
        expect(screen.getByText("Bottom.")).toBeInTheDocument();
        expect(screen.queryByText(/More from the AI summary/)).toBeNull();
    });
});
