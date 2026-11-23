import { describe, expect, test } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AnalysisPanel } from "../analysis-panel";
import { RunAnalysis } from "../run-analysis";
import { planAnalysis, parseDoctorStatus, parseTriage, type RunDoctor } from "@/lib/doctor";
import { readFileSync } from "node:fs";
import path from "node:path";

const MD = "# Analysis\n\n## TL;DR\nTop.\n\n## Findings\nBottom.\n";
const FIX = path.join(__dirname, "..", "..", "..", "..", "lib", "__tests__", "fixtures", "skill-doctor");
const read = (p: string) => readFileSync(path.join(FIX, p), "utf-8");
const doctor = (): RunDoctor => ({
    status: parseDoctorStatus(read("status.json"))!,
    triage: parseTriage(read("triage-1000001.json")),
    fixes: null,
    opened: {},
});

describe("AI summary panel without the doctor block", () => {
    test("the panel renders the whole analysis, Findings included, as before", () => {
        render(<AnalysisPanel markdown={MD} />);
        expect(screen.queryByText("Bottom.")).toBeNull();
        fireEvent.click(screen.getByRole("button", { name: /AI summary/ }));
        expect(screen.getByText("Top.")).toBeInTheDocument();
        expect(screen.getByText("Bottom.")).toBeInTheDocument();
        expect(screen.queryByText(/More from the AI summary/)).toBeNull();
    });
});

describe("RunAnalysis: which panel the page shows", () => {
    test("with a doctor block: one Task failure analysis region, no standalone AI summary", () => {
        render(<RunAnalysis plan={planAnalysis(MD, doctor())} runId="r" sourceId="skills" />);
        expect(screen.getAllByRole("region", { name: "Task failure analysis" })).toHaveLength(1);
        expect(screen.queryByRole("button", { name: /^AI summary/ })).toBeNull();
        expect(screen.getAllByRole("button", { expanded: false })).toHaveLength(1);
    });

    test("without one: the AI summary panel alone", () => {
        render(<RunAnalysis plan={planAnalysis(MD, null)} runId="r" sourceId="skills" />);
        expect(screen.queryByRole("region", { name: "Task failure analysis" })).toBeNull();
        expect(screen.getByRole("button", { name: /AI summary/ })).toBeInTheDocument();
    });

    test("without one and without analysis.md: nothing", () => {
        const { container } = render(<RunAnalysis plan={planAnalysis(null, null)} runId="r" sourceId="skills" />);
        expect(container).toBeEmptyDOMElement();
    });
});
