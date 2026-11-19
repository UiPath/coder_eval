import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import {
    digest,
    groupBySkill,
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

function doctor(patch: Record<string, unknown> = {}): RunDoctor {
    const s = status(patch);
    return {
        status: s,
        triage: s.state === "done" ? parseTriage(read("triage-1000001.json")) : null,
        fixes: parseFixes(read("fixes-1000001/fixes.json")),
    };
}

function renderBlock(d: RunDoctor = doctor(), sourceId = "skills") {
    return render(<TaskFailureAnalysis doctor={d} runId={RUN} sourceId={sourceId} />);
}

const block = () => screen.getByRole("region", { name: "Task failure analysis" });
const header = () => within(block()).getAllByRole("button")[0];
const openBlock = () => fireEvent.click(header());
const skillButton = (skill: string) =>
    screen.getByRole("button", { name: new RegExp(`^▸?▾? ?${skill} `) });
const openSkill = (skill: string) => fireEvent.click(skillButton(skill));

describe("digest", () => {
    test("done: errors and PR-ready fixes", () => {
        expect(digest(doctor())).toBe("5 errors · 1 proposed PR ready");
    });
    test("no PR-ready fix: the errors only", () => {
        const d = doctor();
        d.fixes!.candidates = d.fixes!.candidates.filter((c) => c.outcome !== "pr-ready");
        expect(digest(d)).toBe("5 errors");
        expect(digest({ ...d, fixes: null })).toBe("5 errors");
    });
    test("not done: the state", () => {
        expect(digest(doctor({ state: "running" }))).toMatch(/^running since /);
        expect(digest(doctor({ state: "queued", started_at: null }))).toBe("queued");
        expect(digest(doctor({ state: "failed", reason: "diagnose timed out" }))).toBe(
            "failed: diagnose timed out",
        );
        expect(digest(doctor({ state: "skipped", reason: "ad-hoc run" }))).toBe("skipped: ad-hoc run");
        expect(digest(doctor({ state: "retracted" }))).toBe("the report for this run was retracted");
        expect(digest({ ...doctor(), triage: null })).toMatch(/triage file is missing/);
    });
});

describe("TaskFailureAnalysis block", () => {
    test("collapsed by default, like the AI summary: header and digest only", () => {
        renderBlock();
        expect(header()).toHaveAttribute("aria-expanded", "false");
        expect(header()).toHaveTextContent("Task failure analysis");
        expect(header()).toHaveTextContent("5 errors · 1 proposed PR ready");
        expect(screen.queryByText("Fix first")).toBeNull();
        expect(screen.queryByText("Skill failures")).toBeNull();
    });

    test("not done: no body, the state and the build link", () => {
        renderBlock(doctor({ state: "failed", reason: "boom" }));
        expect(within(block()).queryByRole("button")).toBeNull();
        expect(block()).toHaveTextContent("failed: boom");
        expect(screen.getByRole("link", { name: "build" })).toBeInTheDocument();
    });

    test("body order: stats, layer bar, Fix first, Skill failures", () => {
        renderBlock();
        openBlock();
        const text = block().textContent!;
        const at = (s: string) => text.indexOf(s);
        expect(at("rows run")).toBeGreaterThan(-1);
        expect(at("rows run")).toBeLessThan(at("Failures by layer"));
        expect(at("Failures by layer")).toBeLessThan(at("Fix first"));
        expect(at("Fix first")).toBeLessThan(at("Skill failures"));
        // No separate fix table or task table any more.
        expect(screen.queryByRole("table")).toBeNull();
    });

    test("stats as on the doctor page", () => {
        renderBlock();
        openBlock();
        const stats = screen.getByTestId("doctor-stats");
        expect(stats).toHaveTextContent("10rows run");
        expect(stats).toHaveTextContent("5failed rows");
        expect(stats).toHaveTextContent("3environment");
        expect(stats).toHaveTextContent("2fixable in the repo");
        expect(stats).toHaveTextContent("0agent mistakes");
        expect(screen.getByRole("img", { name: "Failures by layer" }).children).toHaveLength(3);
    });

    test("Fix first: ranked actions, allowlisted links, a PR-ready badge", () => {
        renderBlock();
        openBlock();
        const fixFirst = screen.getByRole("region", { name: "Fix first" });
        const items = within(fixFirst).getAllByRole("listitem");
        expect(items).toHaveLength(3);
        expect(items[0]).toHaveTextContent(/^3 · Environment/);
        expect(within(items[1]).getByRole("link")).toHaveAttribute(
            "href",
            "https://github.com/UiPath/skills/pull/123",
        );
        // javascript: and look-alike hosts stay text.
        expect(within(items[1]).getAllByRole("link")).toHaveLength(1);
        expect(items[1]).toHaveTextContent("javascript:alert(1)");
        expect(within(items[1]).getByTestId("action-ready")).toHaveTextContent("1 PR ready");
        expect(within(items[0]).queryByTestId("action-ready")).toBeNull();
    });

    test("skills: grouped and sorted, all collapsed, with counts and layer dots", () => {
        renderBlock();
        openBlock();
        const a = skillButton("uipath-example");
        const b = skillButton("uipath-example-two");
        expect(a).toHaveAttribute("aria-expanded", "false");
        expect(b).toHaveAttribute("aria-expanded", "false");
        expect(a).toHaveTextContent("2 failed · 1 proposed PR ready");
        expect(a).toHaveTextContent("1 environment");
        expect(a).toHaveTextContent("1 grader");
        expect(b).toHaveTextContent("3 failed");
        expect(b).not.toHaveTextContent("ready");
        expect(screen.queryAllByTestId("task-card")).toHaveLength(0);
    });

    test("env tasks collapse into one block per pre-run check", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example-two");
        const groups = screen.getAllByTestId("env-group");
        expect(groups).toHaveLength(1);
        const btn = within(groups[0]).getByRole("button");
        expect(btn).toHaveTextContent("2 tasks stopped at the pre-run check: connection example-conn");
        fireEvent.click(btn);
        expect(within(groups[0]).getByText(/Reauthorize the connection/)).toBeInTheDocument();
        expect(within(groups[0]).getAllByRole("link").map((l) => l.textContent)).toEqual([
            "example-e2",
            "example-e3",
        ]);
        // The one non-env task is a card.
        expect(screen.getAllByTestId("task-card")).toHaveLength(1);
    });

    test("a card: task link, pills, diagnosis, proposed PR row, dl fields", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example");
        const card = screen
            .getAllByTestId("task-card")
            .find((c) => c.textContent!.includes("example-t1"))!;
        expect(within(card).getByRole("link", { name: "example-t1" })).toHaveAttribute(
            "href",
            `/runs/${RUN}/example-t1`,
        );
        for (const pill of ["Grader", "Fixable: grader", "failure", "high confidence"]) {
            expect(within(card).getByText(pill)).toBeInTheDocument();
        }
        expect(card).toHaveTextContent("The grader expects a literal");
        const fix = within(card).getByTestId("fix-candidate");
        expect(fix).toHaveTextContent("Proposed PR: pr-ready");
        expect(fix).toHaveTextContent("replay pass · mutants 9/10 · review SEND");
        expect(fix).toHaveTextContent("$1.50");
        const patch = within(fix).getByRole("link", { name: "+3/-1" });
        const href = new URL(patch.getAttribute("href")!, "http://x");
        expect(href.pathname).toBe("/api/file");
        expect(href.searchParams.get("path")).toBe(
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        const dts = within(card).getAllByRole("term").map((d) => d.textContent);
        expect(dts).toEqual([
            "Fix",
            "Failing check",
            "Evidence",
            "Passed before",
            "Open PRs on these files",
            "Related skill PRs",
            "Related",
            "Owner",
        ]);
        expect(within(card).getByText("literal").tagName).toBe("CODE");
        expect(within(card).getByRole("link", { name: "#123" })).toBeInTheDocument();
        expect(within(card).getByRole("link", { name: "#125" })).toBeInTheDocument();
        expect(within(card).getByText("javascript:alert(1)").tagName).toBe("SPAN");
        expect(within(card).getByText("Example Owner, Second Owner")).toBeInTheDocument();
    });

    test("a fix candidate that is not ready, and an unsafe patch id", () => {
        const d = doctor();
        d.fixes!.candidates[0].id = "../bad";
        renderBlock(d);
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        const t1 = screen.getAllByTestId("task-card").find((c) => c.textContent!.includes("example-t1"))!;
        expect(within(t1).getByText("+3/-1").closest("a")).toBeNull();
        const t2 = screen.getAllByTestId("task-card").find((c) => c.textContent!.includes("example-t2"))!;
        expect(within(t2).getByTestId("fix-candidate")).toHaveTextContent("Proposed PR: needs-live-run");
    });

    test("a task id the task page would reject is plain text", () => {
        const d = doctor();
        d.triage!.tasks[1].taskId = "../escape";
        renderBlock(d);
        openBlock();
        openSkill("uipath-example");
        expect(screen.getByText("../escape").closest("a")).toBeNull();
    });

    test("Expand all / Collapse all", () => {
        renderBlock();
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        expect(skillButton("uipath-example")).toHaveAttribute("aria-expanded", "true");
        expect(skillButton("uipath-example-two")).toHaveAttribute("aria-expanded", "true");
        fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
        expect(skillButton("uipath-example")).toHaveAttribute("aria-expanded", "false");
    });

    test("layer chips filter, and the last one off shows all again", () => {
        renderBlock();
        openBlock();
        const chips = within(screen.getByRole("toolbar", { name: "Filter failures" }));
        fireEvent.click(chips.getByRole("button", { name: "Skill" }));
        expect(chips.getByRole("button", { name: "Skill" })).toHaveAttribute("aria-pressed", "true");
        expect(chips.getByRole("button", { name: "Grader" })).toHaveAttribute("aria-pressed", "false");
        expect(screen.queryByRole("button", { name: /^▸ uipath-example 2/ })).toBeNull();
        expect(skillButton("uipath-example-two")).toHaveTextContent("1 failed");
        fireEvent.click(chips.getByRole("button", { name: "Skill" }));
        expect(chips.getByRole("button", { name: "Grader" })).toHaveAttribute("aria-pressed", "true");
        expect(skillButton("uipath-example")).toHaveTextContent("2 failed");
    });

    test("search filters and opens the matches", () => {
        renderBlock();
        openBlock();
        fireEvent.change(screen.getByRole("searchbox", { name: "Search failures" }), {
            target: { value: "removed flag" },
        });
        expect(screen.queryByRole("button", { name: /uipath-example 2 failed/ })).toBeNull();
        expect(skillButton("uipath-example-two")).toHaveAttribute("aria-expanded", "true");
        expect(screen.getAllByTestId("task-card")).toHaveLength(1);
        fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zzz-nothing" } });
        expect(screen.getByText("No failures match this filter.")).toBeInTheDocument();
    });

    test("untrusted build URLs are plain text", () => {
        renderBlock(doctor({ state: "running", build_url: "javascript:alert(1)" }));
        expect(screen.queryByRole("link", { name: "build" })).toBeNull();
        expect(screen.getByText("build").tagName).toBe("SPAN");
    });

    test("the source travels in task links", () => {
        renderBlock(doctor(), "gha");
        openBlock();
        openSkill("uipath-example");
        expect(screen.getByRole("link", { name: "example-t1" })).toHaveAttribute(
            "href",
            `/runs/${RUN}/example-t1?src=gha`,
        );
    });
});

describe("groupBySkill", () => {
    test("cards sorted by layer rank, env tasks grouped", () => {
        const g = groupBySkill(doctor(), null, "");
        expect(g.map((x) => x.skill)).toEqual(["uipath-example", "uipath-example-two"]);
        expect(g[0].cards.map((t) => t.taskId)).toEqual(["example-t1"]);
        expect(g[0].envGroups).toEqual([
            { cause: "connection example-conn", tasks: [expect.objectContaining({ taskId: "example-e1" })] },
        ]);
        expect(g[0].ready).toBe(1);
        expect(g[1].ready).toBe(0);
    });
});
