import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import {
    countFixPrs,
    digest,
    groupBySkill,
    proposedPrs,
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
        fixBodies: { "example-grader-fix": read("fixes-1000001/example-grader-fix.md") },
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
    test("done: errors and PRs with fix (prepared + proposed)", () => {
        // example-grader-fix (pr-ready) + open PR #126 named by example-t2's fix.
        expect(digest(doctor())).toBe("5 errors · 2 PRs with fix");
    });
    test("one kind only, and none", () => {
        const d = doctor();
        expect(digest({ ...d, fixes: null })).toBe("5 errors · 1 PR with fix");
        d.triage!.tasks[2].fix = "Document the current flag.";
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
        expect(header()).toHaveTextContent("5 errors · 2 PRs with fix");
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

    test("Fix first: ranked actions, allowlisted links, a PRs-with-fix badge", () => {
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
        expect(within(items[1]).getByTestId("action-ready")).toHaveTextContent("1 PR with fix");
        expect(within(items[0]).queryByTestId("action-ready")).toBeNull();
    });

    test("skills: grouped and sorted, all collapsed, with counts and layer dots", () => {
        renderBlock();
        openBlock();
        const a = skillButton("uipath-example");
        const b = skillButton("uipath-example-two");
        expect(a).toHaveAttribute("aria-expanded", "false");
        expect(b).toHaveAttribute("aria-expanded", "false");
        expect(a).toHaveTextContent("2 failed · 1 PR with fix");
        expect(a).toHaveTextContent("1 environment");
        expect(a).toHaveTextContent("1 grader");
        expect(b).toHaveTextContent("3 failed · 1 PR with fix");
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
        const fix = within(card).getByTestId("pr-with-fix");
        expect(fix).toHaveTextContent("PR with fix");
        expect(fix).toHaveTextContent("fix(example): match the documented output");
        expect(fix).toHaveTextContent("replay pass · mutants 9/10 · review SEND");
        expect(fix).toHaveTextContent("$1.50");
        const patch = within(fix).getByRole("link", { name: "+3/-1" });
        const href = new URL(patch.getAttribute("href")!, "http://x");
        expect(href.pathname).toBe("/api/file");
        expect(href.searchParams.get("path")).toBe(
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
        expect(within(card).queryByTestId("fix-candidate")).toBeNull();
        // #123 is open on these files, but the fix text does not name it.
        expect(within(card).queryByTestId("proposed-pr")).toBeNull();
        const dts = within(card).getAllByRole("term").map((d) => d.textContent);
        expect(dts).toEqual([
            "Fix",
            "Failing check",
            "Evidence",
            "Passed before",
            "Open PRs on these files",
            "Related skill PRs",
            "Related",
        ]);
        expect(within(card).getByText("literal").tagName).toBe("CODE");
        expect(within(card).getByRole("link", { name: "#123" })).toBeInTheDocument();
        expect(within(card).getByRole("link", { name: "#125" })).toBeInTheDocument();
        expect(within(card).getByText("javascript:alert(1)").tagName).toBe("SPAN");
        expect(card).not.toHaveTextContent("Example Owner");
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
        expect(within(t2).getByTestId("fix-candidate")).toHaveTextContent("Fix candidate: needs-live-run");
    });

    test("the PR with fix title expands to the prepared body, then the patch", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example");
        const fix = screen.getByTestId("pr-with-fix");
        expect(within(fix).queryByTestId("pr-body")).toBeNull();
        const title = within(fix).getByRole("button", { name: "fix(example): match the documented output" });
        expect(title).toHaveAttribute("aria-expanded", "false");
        fireEvent.click(title);
        const body = within(fix).getByTestId("pr-body");
        expect(within(body).getByRole("heading", { name: "Summary" })).toBeInTheDocument();
        expect(within(body).getByRole("table")).toBeInTheDocument();
        const links = within(body).getAllByRole("link").map((a) => a.getAttribute("href"));
        expect(links).toEqual([
            "https://github.com/UiPath/skills/pull/123",
            "https://dev.azure.com/example-org/example-project/_build/results?buildId=1000001",
        ]);
        // Off-list and script links are text; images are alt text; raw HTML is dropped.
        expect(within(body).getByText("an off-list link").tagName).toBe("SPAN");
        expect(within(body).getByText("a script link").tagName).toBe("SPAN");
        expect(within(body).queryByRole("img")).toBeNull();
        expect(within(body).getByText("an image")).toBeInTheDocument();
        expect(body.querySelector("b")).toBeNull();
        const dl = within(fix).getByRole("link", { name: "Download patch (+3/-1)" });
        expect(new URL(dl.getAttribute("href")!, "http://x").searchParams.get("path")).toBe(
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
    });

    test("no body: the title is plain text", () => {
        renderBlock({ ...doctor(), fixBodies: {} });
        openBlock();
        openSkill("uipath-example");
        const fix = screen.getByTestId("pr-with-fix");
        expect(within(fix).queryByRole("button")).toBeNull();
        expect(fix).toHaveTextContent("fix(example): match the documented output");
    });

    test("a doctor PR replaces the prepared fix with a GitHub link", () => {
        const d = doctor();
        d.triage!.tasks[1].doctorPr = "https://github.com/UiPath/skills/pull/200";
        renderBlock(d);
        openBlock();
        openSkill("uipath-example");
        const rows = screen.getAllByTestId("pr-with-fix");
        expect(rows).toHaveLength(1);
        expect(rows[0]).toHaveTextContent("PR with fix: #200");
        expect(rows[0]).toHaveAttribute("href", "https://github.com/UiPath/skills/pull/200");
    });

    test("a third-party open PR named in the fix text: a Proposed PR row", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example-two");
        const card = screen.getAllByTestId("task-card").find((c) => c.textContent!.includes("example-t2"))!;
        const rows = within(card).getAllByTestId("proposed-pr");
        // #127 is named too, but merged: it does not count.
        expect(rows).toHaveLength(1);
        expect(rows[0]).toHaveTextContent("Proposed PR#126 docs(example-two): document the current flagopen");
        expect(within(rows[0]).getByRole("link")).toHaveAttribute(
            "href",
            "https://github.com/UiPath/skills/pull/126",
        );
        expect(within(card).getByTestId("fix-candidate")).toHaveTextContent("Fix candidate: needs-live-run");
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
    test("skills sort by PRs ready, then failed tasks, then name", () => {
        // uipath-example: 1 PR with fix of 2 failed; -two: 1 (proposed #126) of 3.
        expect(groupBySkill(doctor(), null, "").map((g) => g.skill)).toEqual([
            "uipath-example-two",
            "uipath-example",
        ]);
        // More PRs with fix wins over more failures.
        const more = doctor();
        more.fixes!.candidates.push({
            ...more.fixes!.candidates[0],
            id: "second-ready",
            taskId: "example-e1",
            covers: ["example-e1"],
        });
        expect(groupBySkill(more, null, "").map((g) => g.skill)).toEqual([
            "uipath-example",
            "uipath-example-two",
        ]);
        // Equal on both: by name.
        const tie = doctor();
        tie.fixes = null;
        tie.triage!.tasks = [
            { ...tie.triage!.tasks[1], skill: "b-skill", taskId: "x1" },
            { ...tie.triage!.tasks[1], skill: "a-skill", taskId: "x2" },
        ];
        expect(groupBySkill(tie, null, "").map((g) => g.skill)).toEqual(["a-skill", "b-skill"]);
    });

    test("cards: own fix, then proposed PR, then other candidates, then the rest", () => {
        const d = doctor();
        const base = d.triage!.tasks[1];
        d.triage!.tasks = [
            { ...base, taskId: "z-plain-skill", layer: "skill", doctorPr: null },
            { ...base, taskId: "a-plain-grader", layer: "grader", doctorPr: null },
            { ...base, taskId: "m-live", layer: "grader", doctorPr: null },
            { ...base, taskId: "y-ready", layer: "grader", doctorPr: null },
            { ...base, taskId: "x-doctor-pr", layer: "grader", doctorPr: "https://github.com/UiPath/skills/pull/7" },
            { ...base, taskId: "b-bad-pr", layer: "grader", doctorPr: "https://example.invalid/pull/7" },
            { ...base, taskId: "p-proposed", layer: "grader", doctorPr: null, fix: "see #123" },
            { ...d.triage!.tasks[0], skill: base.skill },
        ];
        d.fixes!.candidates = [
            { ...d.fixes!.candidates[0], id: "f1", taskId: "y-ready", covers: ["y-ready"] },
            { ...d.fixes!.candidates[1], id: "f2", taskId: "m-live", covers: ["m-live"] },
        ];
        const [g] = groupBySkill(d, null, "");
        expect(g.cards.map((t) => t.taskId)).toEqual([
            "x-doctor-pr",
            "y-ready",
            "p-proposed",
            "m-live",
            // The rest: skill ranks after grader on the doctor page, so first.
            "z-plain-skill",
            "a-plain-grader",
            "b-bad-pr",
        ]);
        // Env tasks stay in their group, rendered after the cards.
        expect(g.envGroups.map((e) => e.tasks.map((t) => t.taskId))).toEqual([["example-e1"]]);
    });

    test("the rendered accordions follow the same order", () => {
        const d = doctor();
        d.triage!.tasks[2].fix = "no PR named";
        renderBlock(d);
        openBlock();
        const order = Array.from(document.querySelectorAll("[data-skill]")).map((e) =>
            e.getAttribute("data-skill"),
        );
        expect(order).toEqual(["uipath-example", "uipath-example-two"]);
    });

    test("cards sorted by layer rank, env tasks grouped", () => {
        const g = groupBySkill(doctor(), null, "");
        const ex = g.find((x) => x.skill === "uipath-example")!;
        expect(ex.cards.map((t) => t.taskId)).toEqual(["example-t1"]);
        expect(ex.envGroups).toEqual([
            { cause: "connection example-conn", tasks: [expect.objectContaining({ taskId: "example-e1" })] },
        ]);
        expect(g.map((x) => x.ready)).toEqual([1, 1]);
    });

    test("proposedPrs: named in the fix text, open, allowlisted", () => {
        const t = doctor().triage!.tasks[2];
        expect(proposedPrs(t).map((p) => p.url)).toEqual(["https://github.com/UiPath/skills/pull/126"]);
        expect(proposedPrs({ ...t, fix: "see https://github.com/UiPath/skills/pull/126" })).toHaveLength(1);
        expect(proposedPrs({ ...t, fix: "see #1260 and #12" })).toHaveLength(0);
        expect(proposedPrs({ ...t, fix: null })).toHaveLength(0);
        expect(
            proposedPrs({
                ...t,
                existingPrs: [{ url: "https://github.com.example.invalid/UiPath/skills/pull/126", title: "", state: "open" }],
            }),
        ).toHaveLength(0);
        expect(
            proposedPrs({ ...t, existingPrs: [{ ...t.existingPrs[0], state: "unknown" }] }),
        ).toHaveLength(0);
    });

    test("countFixPrs counts a PR once across the tasks it covers", () => {
        const d = doctor();
        d.fixes!.candidates[0].covers = ["example-t1", "example-t2"];
        // example-grader-fix covers both; #126 on t2: 2 distinct PRs.
        expect(countFixPrs(d, d.triage!.tasks)).toBe(2);
    });
});
