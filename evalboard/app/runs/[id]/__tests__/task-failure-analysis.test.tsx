import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import {
    countFixPrs,
    digest,
    groupBySkill,
    existingPrFix,
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
// has a needs-live-run candidate, and the diagnosis says open PR #126 fixes it
// (fixed_by_pr "fixes"), which the Fix stage verified (pr_checks).
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
        d.triage!.tasks[2].fixedByPr = null;
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

    test("a verified third-party PR: a Proposed PR row with the evidence", () => {
        renderBlock();
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        const rows = within(c).getAllByTestId("proposed-pr");
        expect(rows).toHaveLength(1);
        expect(rows[0]).toHaveTextContent("Proposed PR#126 docs(example-two): document the current flagopen");
        expect(rows[0]).toHaveTextContent("run_command flips to pass on both");
        expect(within(c).queryByTestId("maybe-pr")).toBeNull();
        expect(within(rows[0]).getByRole("link")).toHaveAttribute(
            "href",
            "https://github.com/UiPath/skills/pull/126",
        );
        expect(within(c).getByTestId("fix-candidate")).toHaveTextContent("Fix candidate: needs-live-run");
    });

    test.each([
        ["untested", undefined],
        ["untested", "needs-windows-run"],
        ["none", undefined],
    ])("check %s (%s): 'Open PR may fix this (not verified)', not counted", (verdict, reason) => {
        const d = doctor();
        if (verdict === "none") d.fixes!.prChecks = {};
        else
            d.fixes!.prChecks["example-t2"] = {
                ...d.fixes!.prChecks["example-t2"]!,
                verdict: "untested",
                evidence: "no stored run could replay it",
                reason: reason ? "needs-windows-run" : null,
            };
        expect(digest(d)).toBe("5 errors");
        renderBlock(d);
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
        const row = within(c).getByTestId("maybe-pr");
        expect(row).toHaveTextContent("Open PR may fix this (not verified)");
        expect(within(row).getByRole("link", { name: /#126/ })).toHaveAttribute(
            "href",
            "https://github.com/UiPath/skills/pull/126",
        );
        expect(row).toHaveTextContent("documents the current flag");
        if (verdict === "none") expect(row).not.toHaveTextContent("replay");
        else expect(row).toHaveTextContent("no stored run could replay it");
        if (reason) expect(row).toHaveTextContent("needs a Windows run");
        else expect(row).not.toHaveTextContent("Windows");
    });

    test.each([
        ["not_verified check", (d: RunDoctor) => { d.fixes!.prChecks["example-t2"]!.verdict = "not_verified"; }],
        ["may_help", (d: RunDoctor) => { d.triage!.tasks[2].fixedByPr!.verdict = "may_help"; }],
        ["unrelated", (d: RunDoctor) => { d.triage!.tasks[2].fixedByPr!.verdict = "unrelated"; }],
        ["merged PR", (d: RunDoctor) => { d.triage!.tasks[2].fixedByPr!.number = 127; }],
        ["PR not in existing_prs", (d: RunDoctor) => { d.triage!.tasks[2].fixedByPr!.number = 999; }],
        ["no fixed_by_pr", (d: RunDoctor) => { d.triage!.tasks[2].fixedByPr = null; }],
    ])("%s: no PR row", (_name, mutate) => {
        const d = doctor();
        mutate(d);
        expect(digest(d)).toBe("5 errors");
        renderBlock(d);
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
        expect(within(c).queryByTestId("maybe-pr")).toBeNull();
    });

    test("a PR only cited in the fix text shows nothing (no prose parsing)", () => {
        const d = doctor();
        // #123 is open on t1's files and named in its fix text, but there is no fixed_by_pr.
        d.triage!.tasks[1].fix = "Open PR #123 (https://github.com/UiPath/skills/pull/123) already fixes this.";
        renderBlock(d);
        openBlock();
        openSkill("uipath-example");
        const c = card("example-t1");
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
        expect(within(c).queryByTestId("maybe-pr")).toBeNull();
    });

    test.each([
        "looks like a relaxation, not a fix: the PR loosens the check",
        "the PR drops the failing criterion run_command: needs review",
        "stored failure on a criterion the yaml no longer has",
    ])("untested evidence %j renders as plain text under 'may fix'", (evidence) => {
        const d = doctor();
        d.fixes!.prChecks["example-t2"] = {
            ...d.fixes!.prChecks["example-t2"]!,
            verdict: "untested",
            evidence,
            headSha: "0123456789abcdef0123456789abcdef01234567",
        };
        renderBlock(d);
        openBlock();
        openSkill("uipath-example-two");
        const row = within(card("example-t2")).getByTestId("maybe-pr");
        expect(row).toHaveTextContent(evidence);
        expect(row).toHaveTextContent("(at 0123456)");
        expect(within(row).getAllByRole("link")).toHaveLength(1);
    });

    test("a malformed check for the task (even a not_verified one) shows nothing", () => {
        const d = doctor();
        // What parseFixes keeps for an entry that did not validate.
        d.fixes!.prChecks["example-t2"] = null;
        expect(existingPrFix(d, d.triage!.tasks[2])).toBeNull();
        const raw = JSON.parse(read("fixes-1000001/fixes.json"));
        raw.pr_checks["example-t2"] = { number: 126, url: "https://evil.example/pull/126", verdict: "not_verified", evidence: "" };
        const d2 = { ...doctor(), fixes: parseFixes(JSON.stringify(raw)) };
        renderBlock(d2);
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        expect(within(c).queryByTestId("maybe-pr")).toBeNull();
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
    });

    test("not-run candidates are hidden and do not lift the sort tier", () => {
        const d = doctor();
        d.fixes!.candidates = d.fixes!.candidates.map((c) => ({
            ...c,
            outcome: "not-run",
            patchLines: null,
            gates: { replay: null, mutants: null, review: null },
        }));
        d.triage!.tasks[2].fixedByPr = null;
        renderBlock(d);
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        expect(block()).not.toHaveTextContent("not-run");
        expect(screen.queryAllByTestId("fix-candidate")).toHaveLength(0);
        expect(screen.queryAllByTestId("fix-prepared")).toHaveLength(0);
        const g = groupBySkill(d, null, "");
        // Nothing lifts either skill: more failed tasks first.
        expect(g.map((x) => x.skill)).toEqual(["uipath-example-two", "uipath-example"]);
        expect(g.every((x) => x.prepared === 0)).toBe(true);
    });

    test("a check for another PR number does not verify this one", () => {
        const d = doctor();
        d.fixes!.prChecks["example-t2"]!.number = 4;
        expect(existingPrFix(d, d.triage!.tasks[2])).toMatchObject({ verified: false, evidence: null });
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

describe("untrusted keys", () => {
    test.each(["constructor", "__proto__", "toString", "hasOwnProperty"])(
        "layer / solvable / opened task id %j do not crash and render as text",
        (k) => {
            const d = doctor();
            d.triage!.tasks[1] = { ...d.triage!.tasks[1], layer: k, solvable: k };
            d.opened = JSON.parse(JSON.stringify({ [k]: OPENED }));
            renderBlock(d);
            openBlock();
            fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
            const c = card("example-t1");
            expect(within(c).getAllByText(k).length).toBeGreaterThan(0);
            expect(screen.getByRole("toolbar", { name: "Filter failures" })).toHaveTextContent(k);
        },
    );
});

describe("prototype-named task ids", () => {
    test.each(["constructor", "__proto__", "toString"])(
        "task id %j: opened.json and pr_checks entries resolve to their own rows",
        (k) => {
            const d = doctor();
            const t1 = { ...d.triage!.tasks[1], taskId: k };
            const t2 = { ...d.triage!.tasks[2], taskId: `${k}-2` };
            d.triage!.tasks = [t1, t2];
            d.fixes!.candidates = [
                { ...d.fixes!.candidates[0], taskId: k, covers: [k] },
            ];
            // As parsed from JSON text, so "__proto__" is an own key.
            d.opened = JSON.parse(JSON.stringify({}).replace("{}", `{"${k}": "${OPENED}"}`));
            d.fixes!.prChecks = JSON.parse(
                `{"${k}-2": {"number": 126, "verdict": "verified", "evidence": "ok", "reason": null, "headSha": null}}`,
            );
            // t1 claims #126 too, but has no check of its own: "may fix".
            t1.fixedByPr = { number: 126, verdict: "fixes", basis: "b" };
            t1.existingPrs = t2.existingPrs;
            renderBlock(d);
            openBlock();
            fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
            const c1 = screen.getAllByTestId("task-card").find((c) => within(c).queryByText(k))!;
            expect(within(c1).getByRole("link", { name: "PR with fix: #300 ↗" })).toBeInTheDocument();
            expect(within(c1).getByTestId("maybe-pr")).toHaveTextContent("#126");
            const c2 = card(`${k}-2`);
            expect(within(c2).getByTestId("proposed-pr")).toHaveTextContent("#126");
            expect(within(c2).queryByTestId("pr-with-fix")).toBeNull();
            // #300 (own) + #126 (verified on the second task).
            expect(digest(d)).toBe("5 errors · 2 PRs with fix");
        },
    );
});

describe("doctor PR resolution", () => {
    test("a task covered by an opened candidate gets that PR, not 'Fix prepared'", () => {
        const d = doctor();
        // example-grader-fix's primary task is example-t1; it also covers t2.
        d.fixes!.candidates[0].covers = ["example-t1", "example-t2"];
        d.opened = { "example-t1": OPENED };
        renderBlock(d);
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        for (const id of ["example-t1", "example-t2"]) {
            const c = card(id);
            expect(within(c).getByRole("link", { name: "PR with fix: #300 ↗" })).toBeInTheDocument();
            expect(within(c).queryByTestId("fix-prepared")).toBeNull();
        }
        // #300 counted once, plus #126 on t2.
        expect(digest(d)).toBe("5 errors · 2 PRs with fix");
    });

    test("the doctor's own PR is not also a Proposed PR, and counts once", () => {
        const d = doctor();
        const pr126 = "https://github.com/UiPath/skills/pull/126";
        d.triage!.tasks[2].doctorPr = pr126;
        renderBlock(d);
        openBlock();
        openSkill("uipath-example-two");
        const c = card("example-t2");
        expect(within(c).getByRole("link", { name: "PR with fix: #126 ↗" })).toBeInTheDocument();
        expect(within(c).queryByTestId("proposed-pr")).toBeNull();
        expect(digest(d)).toBe("5 errors · 1 PR with fix");
    });

    test("no placeholder row for a fixable task without any PR or candidate", () => {
        const d = doctor();
        d.fixes = null;
        d.triage!.tasks[2].fixedByPr = null;
        renderBlock(d);
        openBlock();
        fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
        expect(block()).not.toHaveTextContent("none yet");
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
        d.triage!.tasks[2].fixedByPr = null;
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

    test("cards: own PR, verified PR, prepared fix, unverified PR, other candidates, the rest", () => {
        const d = doctor();
        const base = { ...d.triage!.tasks[1], doctorPr: null, fix: "plain" };
        d.triage!.tasks = [
            { ...base, taskId: "z-plain-skill", layer: "skill" },
            { ...base, taskId: "a-plain-grader", layer: "grader" },
            { ...base, taskId: "m-live", layer: "grader" },
            { ...base, taskId: "y-prepared", layer: "grader" },
            { ...base, taskId: "w-opened", layer: "grader" },
            { ...base, taskId: "x-doctor-pr", layer: "grader", doctorPr: "https://github.com/UiPath/skills/pull/7" },
            { ...base, taskId: "p-proposed", layer: "grader", fixedByPr: { number: 123, verdict: "fixes", basis: "" } },
            { ...base, taskId: "q-maybe", layer: "grader", fixedByPr: { number: 123, verdict: "fixes", basis: "" } },
            { ...d.triage!.tasks[0], skill: base.skill },
        ];
        d.fixes!.candidates = [
            { ...d.fixes!.candidates[0], id: "f1", taskId: "y-prepared", covers: ["y-prepared"] },
            { ...d.fixes!.candidates[0], id: "f3", taskId: "w-opened", covers: ["w-opened"] },
            { ...d.fixes!.candidates[1], id: "f2", taskId: "m-live", covers: ["m-live"] },
        ];
        d.opened = { "w-opened": "https://github.com/UiPath/skills/pull/8" };
        d.fixes!.prChecks = {
            "p-proposed": { number: 123, verdict: "verified", evidence: "", reason: null, headSha: null },
        };
        const [g] = groupBySkill(d, null, "");
        expect(g.cards.map((t) => t.taskId)).toEqual([
            "w-opened",
            "x-doctor-pr",
            "p-proposed",
            "y-prepared",
            "q-maybe",
            "m-live",
            // The rest: skill ranks after grader on the doctor page, so first.
            "z-plain-skill",
            "a-plain-grader",
        ]);
        expect(g.envGroups.map((e) => e.tasks.map((t) => t.taskId))).toEqual([["example-e1"]]);
        // Linked: #8, #7, #123 (verified). The prepared f1 and the unverified q-maybe are not.
        expect(g.fixPrs).toBe(3);
    });

    test("the rendered accordions follow the same order", () => {
        const d = doctor();
        d.triage!.tasks[2].fixedByPr = null;
        renderBlock(d);
        openBlock();
        const order = Array.from(document.querySelectorAll("[data-skill]")).map((e) =>
            e.getAttribute("data-skill"),
        );
        expect(order).toEqual(["uipath-example", "uipath-example-two"]);
    });

    test("countFixPrs counts each linked PR once, and never a prepared fix", () => {
        const d = doctor();
        d.triage!.tasks[1].fixedByPr = d.triage!.tasks[2].fixedByPr;
        d.triage!.tasks[1].existingPrs = d.triage!.tasks[2].existingPrs;
        d.fixes!.prChecks["example-t1"] = d.fixes!.prChecks["example-t2"];
        // t1 and t2 both verified against #126: one PR.
        expect(countFixPrs(d, d.triage!.tasks)).toBe(1);
    });
});
