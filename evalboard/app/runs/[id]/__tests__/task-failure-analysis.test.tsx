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
const OPENED = "https://github.com/UiPath/skills/pull/300";

function status(patch: Record<string, unknown> = {}) {
    return parseDoctorStatus(JSON.stringify({ ...JSON.parse(read("status.json")), ...patch }))!;
}

// The fixture: example-t1 has a pr-ready fix with no PR opened yet; example-t2
// has a needs-live-run candidate and its fix text names open PR #126 (and
// merged #127).
function doctor(patch: Record<string, unknown> = {}): RunDoctor {
    const s = status(patch);
    return {
        status: s,
        triage: s.state === "done" ? parseTriage(read("triage-1000001.json")) : null,
        fixes: parseFixes(read("fixes-1000001/fixes.json")),
        opened: {},
    };
}

function withOpened(d: RunDoctor = doctor()): RunDoctor {
    return { ...d, opened: { "example-t1": OPENED } };
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
const card = (taskId: string) =>
    screen.getAllByTestId("task-card").find((c) => c.textContent!.includes(taskId))!;

describe("digest", () => {
    test("done: errors and linked PRs with fix; a prepared fix does not count", () => {
        // Only #126 (proposed) is linked; example-t1's fix is not opened yet.
        expect(digest(doctor())).toBe("5 errors · 1 PR with fix");
        expect(digest(withOpened())).toBe("5 errors · 2 PRs with fix");
    });
    test("no linked PR: the errors only", () => {
        const d = doctor();
        d.triage!.tasks[2].fix = "Document the current flag.";
        expect(digest(d)).toBe("5 errors");
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
        expect(header()).toHaveTextContent("5 errors · 1 PR with fix");
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
        expect(screen.queryByRole("table")).toBeNull();
    });

    test("no costs anywhere in the panel", () => {
        renderBlock(withOpened());
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        for (const b of within(block()).getAllByTestId("env-group")) {
            fireEvent.click(within(b).getByRole("button"));
        }
        expect(block().textContent).not.toContain("$");
        renderBlock(doctor({ state: "running" }));
        expect(document.body.textContent).not.toContain("$");
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

    test("Fix first: env and layer actions only, allowlisted links, a PRs-with-fix badge", () => {
        const d = withOpened();
        d.triage!.actions[1].text +=
            " See https://github.com/UiPath/skills/pull/123, javascript:alert(1) and https://github.com.example.invalid/x.";
        renderBlock(d);
        openBlock();
        const items = within(screen.getByRole("region", { name: "Fix first" })).getAllByRole("listitem");
        // The fixture's open-pr action is left out.
        expect(items).toHaveLength(2);
        expect(block()).not.toHaveTextContent("touches the files");
        expect(items[0]).toHaveTextContent(/^3 · Environment/);
        expect(within(items[1]).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual([
            "https://github.com/UiPath/skills/pull/123",
        ]);
        expect(items[1]).toHaveTextContent("javascript:alert(1)");
        expect(within(items[1]).getByTestId("action-ready")).toHaveTextContent("1 PR with fix");
        expect(within(items[0]).queryByTestId("action-ready")).toBeNull();
    });

    test("a prepared fix gives no badge", () => {
        renderBlock();
        openBlock();
        expect(screen.queryByTestId("action-ready")).toBeNull();
    });

    test("no env or layer actions: no Fix first section", () => {
        const d = doctor();
        d.triage!.actions = [];
        renderBlock(d);
        openBlock();
        expect(screen.queryByRole("region", { name: "Fix first" })).toBeNull();
    });

    test("skills: grouped, all collapsed, with linked-PR counts and layer dots", () => {
        renderBlock();
        openBlock();
        const a = skillButton("uipath-example");
        const b = skillButton("uipath-example-two");
        expect(a).toHaveAttribute("aria-expanded", "false");
        expect(b).toHaveAttribute("aria-expanded", "false");
        // example-t1's fix is only prepared: it does not count.
        expect(a).toHaveTextContent("2 failed");
        expect(a).not.toHaveTextContent("with fix");
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
        expect(screen.getAllByTestId("task-card")).toHaveLength(1);
    });

    test("a card: task link, pills, diagnosis, dl fields; no file-touching PRs, no owner", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example");
        const c = card("example-t1");
        expect(within(c).getByRole("link", { name: "example-t1" })).toHaveAttribute(
            "href",
            `/runs/${RUN}/example-t1`,
        );
        for (const pill of ["Grader", "Fixable: grader", "failure", "high confidence"]) {
            expect(within(c).getByText(pill)).toBeInTheDocument();
        }
        expect(c).toHaveTextContent("The grader expects a literal");
        expect(within(c).getAllByRole("term").map((d) => d.textContent)).toEqual([
            "Fix",
            "Failing check",
            "Evidence",
            "Passed before",
            "Related",
        ]);
        expect(within(c).getByText("literal").tagName).toBe("CODE");
        // #123 (open on these files) and #125 (related skill PR) are not shown.
        expect(c).not.toHaveTextContent("#123");
        expect(c).not.toHaveTextContent("#125");
        expect(c).not.toHaveTextContent("Example Owner");
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
    });

    test("a pr-ready fix with no PR opened: plain text, patch and gates, no link", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example");
        const c = card("example-t1");
        expect(within(c).queryByTestId("pr-with-fix")).toBeNull();
        const row = within(c).getByTestId("fix-prepared");
        expect(row).toHaveTextContent("Fix prepared, PR not opened yet");
        expect(row).toHaveTextContent("replay pass · mutants 9/10 · review SEND");
        expect(within(row).getAllByRole("link")).toHaveLength(1);
        const patch = within(row).getByRole("link", { name: "+3/-1" });
        const href = new URL(patch.getAttribute("href")!, "http://x");
        expect(href.pathname).toBe("/api/file");
        expect(href.searchParams.get("path")).toBe(
            "skill-doctor/fixes-1000001/example-grader-fix.patch",
        );
    });

    test("an opened PR (opened.json): 'PR with fix: #N ↗' with the gates", () => {
        renderBlock(withOpened());
        openBlock();
        openSkill("uipath-example");
        const c = card("example-t1");
        const row = within(c).getByTestId("pr-with-fix");
        expect(within(row).getByRole("link", { name: "PR with fix: #300 ↗" })).toHaveAttribute(
            "href",
            OPENED,
        );
        expect(row).toHaveTextContent("replay pass · mutants 9/10 · review SEND");
        expect(within(c).queryByTestId("fix-prepared")).toBeNull();
    });

    test("a doctor PR from the triage counts the same way", () => {
        const d = doctor();
        d.triage!.tasks[1].doctorPr = "https://github.com/UiPath/skills/pull/200";
        expect(digest(d)).toBe("5 errors · 2 PRs with fix");
        renderBlock(d);
        openBlock();
        openSkill("uipath-example");
        expect(
            within(card("example-t1")).getByRole("link", { name: "PR with fix: #200 ↗" }),
        ).toBeInTheDocument();
    });

    test("a third-party open PR named in the fix text: a Proposed PR row", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        const rows = within(c).getAllByTestId("proposed-pr");
        // #127 is named too, but merged: it does not count.
        expect(rows).toHaveLength(1);
        expect(rows[0]).toHaveTextContent("Proposed PR#126 docs(example-two): document the current flagopen");
        expect(within(rows[0]).getByRole("link")).toHaveAttribute(
            "href",
            "https://github.com/UiPath/skills/pull/126",
        );
        expect(within(c).getByTestId("fix-candidate")).toHaveTextContent("Fix candidate: needs-live-run");
    });

    test("an unsafe patch id shows its size without a link", () => {
        const d = doctor();
        d.fixes!.candidates[0].id = "../bad";
        renderBlock(d);
        openBlock();
        openSkill("uipath-example");
        expect(within(card("example-t1")).getByText("+3/-1").closest("a")).toBeNull();
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
    test("skills: linked PRs first, then prepared fixes, then failed tasks, then name", () => {
        // -two: 1 linked (#126) of 3; uipath-example: 0 linked, 1 prepared, of 2.
        expect(groupBySkill(doctor(), null, "").map((g) => g.skill)).toEqual([
            "uipath-example-two",
            "uipath-example",
        ]);
        // Without #126, the prepared fix beats more failed tasks.
        const d = doctor();
        d.triage!.tasks[2].fix = "no PR named";
        expect(groupBySkill(d, null, "").map((g) => g.skill)).toEqual([
            "uipath-example",
            "uipath-example-two",
        ]);
        // Nothing at all: more failed tasks first.
        const none = { ...d, fixes: null };
        expect(groupBySkill(none, null, "").map((g) => g.skill)).toEqual([
            "uipath-example-two",
            "uipath-example",
        ]);
        // Equal on everything: by name.
        const tie = { ...none, triage: { ...none.triage!, tasks: [
            { ...none.triage!.tasks[1], skill: "b-skill", taskId: "x1" },
            { ...none.triage!.tasks[1], skill: "a-skill", taskId: "x2" },
        ] } };
        expect(groupBySkill(tie, null, "").map((g) => g.skill)).toEqual(["a-skill", "b-skill"]);
    });

    test("cards: own PR, proposed PR, prepared fix, other candidates, the rest", () => {
        const d = doctor();
        const base = { ...d.triage!.tasks[1], doctorPr: null, fix: "plain" };
        d.triage!.tasks = [
            { ...base, taskId: "z-plain-skill", layer: "skill" },
            { ...base, taskId: "a-plain-grader", layer: "grader" },
            { ...base, taskId: "m-live", layer: "grader" },
            { ...base, taskId: "y-prepared", layer: "grader" },
            { ...base, taskId: "w-opened", layer: "grader" },
            { ...base, taskId: "x-doctor-pr", layer: "grader", doctorPr: "https://github.com/UiPath/skills/pull/7" },
            { ...base, taskId: "b-bad-pr", layer: "grader", doctorPr: "https://example.invalid/pull/7" },
            { ...base, taskId: "p-proposed", layer: "grader", fix: "see #123" },
            { ...d.triage!.tasks[0], skill: base.skill },
        ];
        d.fixes!.candidates = [
            { ...d.fixes!.candidates[0], id: "f1", taskId: "y-prepared", covers: ["y-prepared"] },
            { ...d.fixes!.candidates[0], id: "f3", taskId: "w-opened", covers: ["w-opened"] },
            { ...d.fixes!.candidates[1], id: "f2", taskId: "m-live", covers: ["m-live"] },
        ];
        d.opened = { "w-opened": "https://github.com/UiPath/skills/pull/8" };
        const [g] = groupBySkill(d, null, "");
        expect(g.cards.map((t) => t.taskId)).toEqual([
            "w-opened",
            "x-doctor-pr",
            "p-proposed",
            "y-prepared",
            "m-live",
            // The rest: skill ranks after grader on the doctor page, so first.
            "z-plain-skill",
            "a-plain-grader",
            "b-bad-pr",
        ]);
        expect(g.envGroups.map((e) => e.tasks.map((t) => t.taskId))).toEqual([["example-e1"]]);
        // Linked: #8, #7, #123. The prepared f1 is not.
        expect(g.ready).toBe(3);
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
        expect(proposedPrs({ ...t, existingPrs: [{ ...t.existingPrs[0], state: "unknown" }] })).toHaveLength(0);
    });

    test("countFixPrs counts each linked PR once, and never a prepared fix", () => {
        const d = doctor();
        d.triage!.tasks[1].fix = "see #126";
        d.triage!.tasks[1].existingPrs = d.triage!.tasks[2].existingPrs;
        // t1 and t2 both name #126: one PR.
        expect(countFixPrs(d, d.triage!.tasks)).toBe(1);
    });
});
