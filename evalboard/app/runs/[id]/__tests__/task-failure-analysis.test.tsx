import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { render, screen, within } from "@testing-library/react";
import {
    DoctorStatusLine,
    TaskFailureAnalysis,
} from "../task-failure-analysis";
import {
    parseDoctorStatus,
    parseFixes,
    parseTriage,
    type RunDoctor,
} from "@/lib/doctor";

const FIX = path.join(__dirname, "..", "..", "..", "..", "lib", "__tests__", "fixtures", "skill-doctor");
const read = (p: string) => readFileSync(path.join(FIX, p), "utf-8");
const RUN = "2099-01-03_04-23-30";

function status(patch: Record<string, unknown> = {}) {
    return parseDoctorStatus(JSON.stringify({ ...JSON.parse(read("status.json")), ...patch }))!;
}

function doctor(): RunDoctor {
    return {
        status: status(),
        triage: parseTriage(read("triage-1000001.json")),
        fixes: parseFixes(read("fixes-1000001/fixes.json")),
    };
}

describe("TaskFailureAnalysis", () => {
    test("header: counts by layer, class and task state, history, cost, build link", () => {
        render(<TaskFailureAnalysis doctor={doctor()} runId={RUN} sourceId="skills" />);
        const panel = screen.getByRole("region", { name: "Task Failure Analysis" });
        expect(panel).toHaveTextContent("3 failed, 2 diagnosed");
        expect(panel).toHaveTextContent("new 2");
        expect(panel).toHaveTextContent("known 1");
        expect(panel).toHaveTextContent("vs 2099-01-02_04-25-52: 2 new, 1 persisting, 0 fixed");
        expect(panel).toHaveTextContent("diagnosis $1.25");
        expect(panel).toHaveTextContent("grader 1");
        expect(panel).toHaveTextContent("failure 2");
        expect(screen.getByRole("link", { name: "build" })).toHaveAttribute(
            "href",
            "https://dev.azure.com/example-org/example-project/_build/results?buildId=1000001",
        );
        expect(screen.getByRole("link", { name: "nightly" })).toBeInTheDocument();
    });

    test("task rows link to the task page; only real skills PR URLs are links", () => {
        render(<TaskFailureAnalysis doctor={doctor()} runId={RUN} sourceId="skills" />);
        const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
        expect(links).toContain(`/runs/${RUN}/example-t1`);
        expect(links).toContain("https://github.com/UiPath/skills/pull/123");
        expect(links.some((h) => h?.startsWith("javascript:"))).toBe(false);
        expect(links.some((h) => h?.includes("example.invalid"))).toBe(false);
        // The rejected URLs are still visible, as text.
        expect(screen.getByText("javascript:alert(1)").tagName).toBe("SPAN");
        expect(
            screen.getByText("https://github.com.example.invalid/UiPath/skills/pull/124").tagName,
        ).toBe("SPAN");
        expect(screen.getByText("Example Owner, Second Owner")).toBeInTheDocument();
    });

    test("the source travels in the task link", () => {
        render(<TaskFailureAnalysis doctor={doctor()} runId={RUN} sourceId="gha" />);
        const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
        expect(links).toContain(`/runs/${RUN}/example-t1?src=gha`);
    });

    test("actions and fixes", () => {
        render(<TaskFailureAnalysis doctor={doctor()} runId={RUN} sourceId="skills" />);
        expect(screen.getByText(/1 task diagnosed as a grader problem/)).toBeInTheDocument();
        const patch = screen.getByRole("link", { name: "+3/-1" });
        const href = new URL(patch.getAttribute("href")!, "http://x");
        expect(href.pathname).toBe("/api/file");
        expect(href.searchParams.get("run")).toBe(RUN);
        expect(href.searchParams.get("path")).toBe(
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        const row = patch.closest("tr")!;
        expect(within(row).getByText("replay pass · mutants 9/10 · review SEND")).toBeInTheDocument();
        expect(within(row).getByText("2")).toBeInTheDocument();
        expect(screen.getByText("needs-live-run")).toBeInTheDocument();
    });

    test("untrusted build URLs are plain text", () => {
        const d = doctor();
        d.status = status({ build_url: "javascript:alert(1)", trigger: "https://dev.azure.com.example.invalid/x" });
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.queryByRole("link", { name: "build" })).toBeNull();
        expect(screen.queryByRole("link", { name: "nightly" })).toBeNull();
        expect(screen.getByText("build").tagName).toBe("SPAN");
    });

    test("no base run: no history line", () => {
        const d = doctor();
        d.triage!.summary.vsHistory = null;
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.queryByText(/^vs /)).toBeNull();
    });

    test("a task id the task page would reject is plain text, not a link", () => {
        const d = doctor();
        d.triage!.tasks[0].taskId = "../escape";
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.getByText("../escape").closest("a")).toBeNull();
        const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
        expect(links.some((h) => h?.includes("escape"))).toBe(false);
    });

    test("a patch whose id is not a safe file name shows its size without a link", () => {
        const d = doctor();
        d.fixes!.candidates[0].id = "../bad";
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.getByText("+3/-1").closest("a")).toBeNull();
    });

    test("no candidates", () => {
        const d = doctor();
        d.fixes!.candidates = [];
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.getByText("No fix candidates.")).toBeInTheDocument();
    });

    test("no fixes.json: no Fixes table", () => {
        const d = { ...doctor(), fixes: null };
        render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId="skills" />);
        expect(screen.queryByText("Fixes")).toBeNull();
    });
});

describe("DoctorStatusLine", () => {
    test("running: start time and build link", () => {
        render(<DoctorStatusLine status={status({ state: "running" })} />);
        const line = screen.getByTestId("doctor-status-line");
        expect(line).toHaveTextContent("running since");
        expect(within(line).getByRole("link", { name: "build" })).toBeInTheDocument();
    });

    test("queued", () => {
        render(<DoctorStatusLine status={status({ state: "queued" })} />);
        expect(screen.getByTestId("doctor-status-line")).toHaveTextContent("queued since");
    });

    test("failed and skipped show the reason", () => {
        const { rerender } = render(
            <DoctorStatusLine status={status({ state: "failed", reason: "diagnose timed out" })} />,
        );
        expect(screen.getByTestId("doctor-status-line")).toHaveTextContent("failed: diagnose timed out");
        rerender(<DoctorStatusLine status={status({ state: "skipped", reason: "ad-hoc run" })} />);
        expect(screen.getByTestId("doctor-status-line")).toHaveTextContent("skipped: ad-hoc run");
    });

    test("done without a triage says why there is no panel", () => {
        render(<DoctorStatusLine status={status()} />);
        expect(screen.getByTestId("doctor-status-line")).toHaveTextContent(
            "report done, but its triage file is missing or did not parse",
        );
    });

    test("retracted is a one-line note", () => {
        render(<DoctorStatusLine status={status({ state: "retracted" })} />);
        expect(screen.getByTestId("doctor-status-line")).toHaveTextContent("retracted");
    });
});
