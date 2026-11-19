import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import {
    isAdoUrl,
    isLinkableTaskId,
    patchIdFromPath,
    isSkillsPrUrl,
    parseDoctorStatus,
    parseFixes,
    parseTriage,
    patchIds,
    planAnalysis,
    splitAnalysis,
    taskAnalysisEnabled,
    type RunDoctor,
} from "../doctor";

const FIX = path.join(__dirname, "fixtures", "skill-doctor");
const read = (p: string) => readFileSync(path.join(FIX, p), "utf-8");
const STATUS = read("status.json");
const TRIAGE = read("triage-1000001.json");
const FIXES = read("fixes-1000001/fixes.json");

function statusWith(patch: Record<string, unknown>): string {
    return JSON.stringify({ ...JSON.parse(STATUS), ...patch });
}

function doctor(patch: Record<string, unknown> = {}): RunDoctor {
    const status = parseDoctorStatus(statusWith(patch))!;
    return {
        status,
        triage: status.state === "done" ? parseTriage(TRIAGE) : null,
        fixes: parseFixes(FIXES),
    };
}

const MD = [
    "# Analysis",
    "",
    "## TL;DR",
    "Top part.",
    "",
    "## Findings",
    "Bottom part.",
    "",
    "## Quick Wins",
    "More.",
    "",
].join("\n");

describe("splitAnalysis", () => {
    test("no heading: no split", () => {
        expect(splitAnalysis("# A\n\n## TL;DR\ntext\n")).toBeNull();
    });

    test("one heading: cuts at the Findings line", () => {
        const s = splitAnalysis(MD)!;
        expect(s.top).toBe("# Analysis\n\n## TL;DR\nTop part.\n\n");
        expect(s.findings.startsWith("## Findings\nBottom part.")).toBe(true);
        expect(s.top + s.findings).toBe(MD);
    });

    test("cuts at the first of two", () => {
        const s = splitAnalysis("a\n## Findings\nb\n## Findings\nc")!;
        expect(s.top).toBe("a\n");
        expect(s.findings).toBe("## Findings\nb\n## Findings\nc");
    });

    test("a Findings line inside a fenced code block is not a split point", () => {
        const md = "x\n```md\n## Findings\n```\ny\n~~~\n## Findings\n~~~\nz\n";
        expect(splitAnalysis(md)).toBeNull();
        const s = splitAnalysis(`${md}## Findings\nreal\n`)!;
        expect(s.findings).toBe("## Findings\nreal\n");
    });

    test("### Findings, a suffix or leading spaces are not split points", () => {
        expect(splitAnalysis("### Findings\n")).toBeNull();
        expect(splitAnalysis("## Findings (3)\n")).toBeNull();
        expect(splitAnalysis(" ## Findings\n")).toBeNull();
        expect(splitAnalysis("## Findings \n")).toBeNull();
    });

    test("CRLF line endings still split", () => {
        const s = splitAnalysis("a\r\n## Findings\r\nb")!;
        expect(s.top).toBe("a\r\n");
    });
});

describe("parseDoctorStatus", () => {
    test("reads the fixture", () => {
        const s = parseDoctorStatus(STATUS)!;
        expect(s.state).toBe("done");
        expect(s.triageFile).toBe("triage-1000001.json");
        expect(s.fixesDir).toBe("fixes-1000001");
        expect(s.costUsd).toBe(1.25);
    });

    test("schema_version 2 is absent", () => {
        expect(parseDoctorStatus(statusWith({ schema_version: 2 }))).toBeNull();
    });

    test("unparsable JSON, a non-object or an unknown state is absent", () => {
        expect(parseDoctorStatus("{not json")).toBeNull();
        expect(parseDoctorStatus("[1]")).toBeNull();
        expect(parseDoctorStatus(null)).toBeNull();
        expect(parseDoctorStatus(statusWith({ state: "exploded" }))).toBeNull();
    });

    test("a bad triage_file name is treated as absent", () => {
        for (const bad of [
            "../run.json",
            "triage-1/../../x.json",
            "summary.json",
            "triage-.json",
            "triage-1.json.bak",
        ]) {
            expect(parseDoctorStatus(statusWith({ triage_file: bad }))!.triageFile).toBeNull();
        }
    });

    test("a bad fixes_dir is treated as absent; the old shape has none", () => {
        expect(parseDoctorStatus(statusWith({ fixes_dir: "../x" }))!.fixesDir).toBeNull();
        expect(parseDoctorStatus(statusWith({ fixes_dir: "fixes" }))!.fixesDir).toBe("fixes");
        const old = JSON.parse(STATUS);
        delete old.fixes_dir;
        delete old.fixes;
        delete old.trigger;
        const s = parseDoctorStatus(JSON.stringify(old))!;
        expect(s.fixesDir).toBeNull();
        expect(s.trigger).toBeNull();
    });
});

describe("parseTriage / parseFixes", () => {
    test("reads the fixture: the card fields", () => {
        const t = parseTriage(TRIAGE)!;
        expect(t.tasks.map((x) => x.taskId)).toEqual([
            "example-e1",
            "example-t1",
            "example-t2",
            "example-e2",
            "example-e3",
        ]);
        expect(t.rowsRun).toBe(10);
        expect(t.rowsFailed).toBe(5);
        expect(t.actions[0]).toEqual({
            count: 3,
            text: expect.stringContaining("Reauthorize"),
            taskIds: ["example-e1", "example-e2", "example-e3"],
        });
        const t1 = t.tasks[1];
        expect(t1).toMatchObject({
            layer: "grader",
            solvable: "yes-grader",
            runStatus: "FAILURE",
            confidence: "high",
            criterion: "file_contains",
            passedBefore: "2099-01-01_04-20-00 (claude-code)",
            related: "example-t3 fails the same check",
            owners: ["Example Owner", "Second Owner"],
            envGroup: null,
            doctorPr: null,
        });
        expect(t1.existingPrs).toHaveLength(3);
        expect(t1.relatedSkillPrs[0].url).toBe("https://github.com/UiPath/skills/pull/125");
        expect(t.tasks[0].envGroup).toBe("connection example-conn");
    });

    test("schema_version 2 or a broken triage is absent", () => {
        expect(parseTriage(JSON.stringify({ ...JSON.parse(TRIAGE), schema_version: 2 }))).toBeNull();
        expect(parseTriage("{")).toBeNull();
        expect(parseTriage(JSON.stringify({ schema_version: 1 }))).toBeNull();
    });

    test("fixes.json with schema_version 2, or none, is absent", () => {
        const doc = JSON.parse(FIXES);
        expect(parseFixes(JSON.stringify({ ...doc, schema_version: 2 }))).toBeNull();
        delete doc.schema_version;
        expect(parseFixes(JSON.stringify(doc))).toBeNull();
    });

    test("fixes: candidates, gates and patch ids", () => {
        const f = parseFixes(FIXES)!;
        expect(f.candidates.map((c) => c.outcome)).toEqual(["pr-ready", "needs-live-run"]);
        expect(f.candidates[0].gates).toEqual({ replay: "pass", mutants: { n: 10, killed: 9 }, review: "SEND" });
        expect([...patchIds(f)]).toEqual(["example-grader-fix"]);
    });

    test("a candidate id that is not a safe file name gets no patch", () => {
        const doc = JSON.parse(FIXES);
        doc.candidates[0].id = "../../run";
        expect(patchIds(parseFixes(JSON.stringify(doc))).size).toBe(0);
    });

    test("patchIdFromPath", () => {
        expect(patchIdFromPath("fixes-1/a.patch")).toEqual({ fixesDir: "fixes-1", id: "a" });
        expect(patchIdFromPath("fixes/a.b.patch")).toEqual({ fixesDir: "fixes", id: "a.b" });
        for (const bad of ["fixes-1/fixes.json", "fixes-1/.patch", "x/a.patch", "fixes-1/../a.patch", "a.patch", "fixes-1/a/b.patch"]) {
            expect(patchIdFromPath(bad)).toBeNull();
        }
    });
});

describe("link validation", () => {
    test("skills PR links", () => {
        expect(isSkillsPrUrl("https://github.com/UiPath/skills/pull/123")).toBe(true);
        for (const bad of [
            "javascript:alert(1)",
            "http://github.com/UiPath/skills/pull/123",
            "https://github.com/UiPath/skills/pull/123/files",
            "https://github.com/UiPath/skills/pull/123?x=1",
            "https://github.com.example.invalid/UiPath/skills/pull/123",
            "https://github.com/UiPath/skills-fork/pull/123",
            "https://github.com/Other/skills/pull/123",
            "https://evil.example/https://github.com/UiPath/skills/pull/1",
            " https://github.com/UiPath/skills/pull/123",
        ]) {
            expect(isSkillsPrUrl(bad)).toBe(false);
        }
    });

    test("build links", () => {
        expect(isAdoUrl("https://dev.azure.com/example-org/example-project/_build/results?buildId=1")).toBe(true);
        for (const bad of [
            "javascript:alert(1)",
            "http://dev.azure.com/example-org/x",
            "https://dev.azure.com.example.invalid/x",
            "https://dev.azure.com@example.invalid/x",
            "https://example.invalid/dev.azure.com/x",
            "https://dev.azure.com/x\"onmouseover=\"y",
            null,
        ]) {
            expect(isAdoUrl(bad)).toBe(false);
        }
    });
});

describe("isLinkableTaskId", () => {
    test("accepts the ids the task page accepts", () => {
        expect(isLinkableTaskId("example-t1")).toBe(true);
        expect(isLinkableTaskId("suite/row-3")).toBe(true);
    });
    test("rejects path tricks, URLs and odd characters", () => {
        for (const bad of ["", "../x", "a/../b", "a/./b", "/abs", "a//b", "https://x", "a b", "a?b", "a#b", "x".repeat(256)]) {
            expect(isLinkableTaskId(bad)).toBe(false);
        }
    });
});

describe("taskAnalysisEnabled (gating + kill switch)", () => {
    test("internal + skills + switch not off", () => {
        expect(taskAnalysisEnabled(true, "skills", undefined)).toBe(true);
        expect(taskAnalysisEnabled(true, "skills", "")).toBe(true);
        expect(taskAnalysisEnabled(true, "skills", "on")).toBe(true);
    });
    test("off, OSS or another source", () => {
        expect(taskAnalysisEnabled(true, "skills", "off")).toBe(false);
        expect(taskAnalysisEnabled(true, "skills", "OFF")).toBe(false);
        expect(taskAnalysisEnabled(true, "skills", " Off ")).toBe(false);
        expect(taskAnalysisEnabled(false, "skills", undefined)).toBe(false);
        expect(taskAnalysisEnabled(true, "scribe", undefined)).toBe(false);
        expect(taskAnalysisEnabled(true, "gha", undefined)).toBe(false);
    });
});

describe("planAnalysis (states)", () => {
    test("absent: today's view, no block", () => {
        expect(planAnalysis(MD, null)).toEqual({ summary: MD, doctor: null });
        expect(planAnalysis(null, null)).toEqual({ summary: null, doctor: null });
    });

    test("done with a split: summary keeps the top, the block replaces the rest", () => {
        const d = doctor();
        const p = planAnalysis(MD, d);
        expect(p.summary).toBe("# Analysis\n\n## TL;DR\nTop part.\n\n");
        expect(p.doctor).toBe(d);
    });

    test("done without a split: the full analysis, then the block", () => {
        const md = "# A\n\n## TL;DR\nonly\n";
        const d = doctor();
        expect(planAnalysis(md, d)).toEqual({ summary: md, doctor: d });
    });

    test("done without analysis.md: the block alone", () => {
        const d = doctor();
        expect(planAnalysis(null, d)).toEqual({ summary: null, doctor: d });
    });

    test("an analysis.md that starts with Findings: no summary", () => {
        const d = doctor();
        expect(planAnalysis("## Findings\nx\n", d)).toEqual({ summary: null, doctor: d });
    });

    test("done but the triage did not load: the full analysis", () => {
        const d = { ...doctor(), triage: null };
        expect(planAnalysis(MD, d)).toEqual({ summary: MD, doctor: d });
    });

    test.each(["queued", "running", "failed", "skipped", "retracted"])(
        "%s: the full analysis (Findings included) and the block",
        (state) => {
            const d = doctor({ state, reason: "why" });
            expect(planAnalysis(MD, d)).toEqual({ summary: MD, doctor: d });
        },
    );
});
