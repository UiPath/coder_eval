import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { LAYER_ORDER } from "@/app/runs/[id]/task-failure-analysis";
import { DOCTOR_STATES } from "../doctor";

// The board's parsers (lib/doctor.ts) are written by hand against the doctor's
// contract v1. These tests pin them to copies of the producer's schemas
// (fixtures/skill-doctor/schemas/, see its README): a field the parsers read
// must be declared there, and the hand-written fixtures may use only declared
// keys. A rename on the producer side then fails here once the copies are
// refreshed, instead of silently dropping a column on the board.

type Schema = Record<string, unknown>;

const FIX = path.join(__dirname, "fixtures", "skill-doctor");
const load = (p: string) => JSON.parse(readFileSync(path.join(FIX, p), "utf-8"));
const SCHEMAS: Record<string, Schema> = {
    status: load("schemas/status_v1.json"),
    triage: load("schemas/triage_v1.json"),
    fixes: load("schemas/fixes_v1.json"),
};

function deref(root: Schema, node: unknown): Schema | null {
    let n = node;
    while (n && typeof n === "object" && "$ref" in n) {
        const ref = (n as Schema).$ref as string;
        n = (root.$defs as Schema)[ref.replace("#/$defs/", "")];
    }
    return n && typeof n === "object" ? (n as Schema) : null;
}

// "a.[].b": `[]` steps into array items, `*` into additionalProperties.
function nodeAt(root: Schema, p: string): Schema | null {
    let node: Schema | null = root;
    for (const seg of p.split(".")) {
        node = deref(root, node);
        if (!node) return null;
        if (seg === "[]") node = deref(root, node.items);
        else if (seg === "*") node = deref(root, node.additionalProperties);
        else node = deref(root, (node.properties as Schema | undefined)?.[seg]);
    }
    return node;
}

// Every field the parsers in lib/doctor.ts read.
const READS: Record<string, string[]> = {
    status: [
        "schema_version", "state", "reason", "started_at", "build_url",
        "triage_file", "fixes_dir", "trigger",
    ],
    triage: [
        "schema_version", "run.run_id", "run.rows_executed",
        "summary.rows_failed", "summary.rows_diagnosed",
        ...[
            "task_id", "skill", "layer", "solvable", "run_status", "confidence",
            "why", "fix", "criterion", "evidence", "passed_before", "env_group",
            "doctor_pr", "existing_prs.[].url", "existing_prs.[].title",
            "existing_prs.[].state", "fixed_by_pr.number", "fixed_by_pr.verdict",
            "fixed_by_pr.basis",
        ].map((f) => `tasks.[].${f}`),
        "actions.[].kind", "actions.[].count", "actions.[].text", "actions.[].task_ids",
    ],
    fixes: [
        "schema_version", "fix_mode", "candidates.[].outcome",
        ...["number", "verdict", "url", "evidence", "reason", "head_sha"].map(
            (f) => `pr_checks.*.${f}`,
        ),
    ],
};

describe("doctor contract: the parsers read only declared fields", () => {
    test.each(Object.entries(READS).flatMap(([s, ps]) => ps.map((p) => [s, p])))(
        "%s_v1 declares %s",
        (s, p) => {
            expect(nodeAt(SCHEMAS[s], p)).not.toBeNull();
        },
    );

    test("fixes_v1 requires the candidate keys the board needs", () => {
        const items = deref(SCHEMAS.fixes, nodeAt(SCHEMAS.fixes, "candidates")?.items);
        expect(items?.required).toEqual(expect.arrayContaining(["id", "task_id", "outcome"]));
    });
});

const enumAt = (s: string, p: string) => nodeAt(SCHEMAS[s], p)?.enum as string[];

describe("doctor contract: closed value sets match the producer", () => {
    test("status states", () => {
        expect([...DOCTOR_STATES].sort()).toEqual([...enumAt("status", "state")].sort());
    });

    test("every layer has a label, and only those", () => {
        expect([...LAYER_ORDER].sort()).toEqual([...enumAt("triage", "tasks.[].layer")].sort());
    });

    test("the action kind the board leaves out still exists", () => {
        expect(enumAt("triage", "actions.[].kind")).toContain("open-pr");
    });

    test("PR verdicts", () => {
        expect([...enumAt("triage", "tasks.[].fixed_by_pr.verdict")].sort()).toEqual(
            ["fixes", "may_help", "unrelated"],
        );
        expect([...enumAt("fixes", "pr_checks.*.verdict")].sort()).toEqual(
            ["not_verified", "untested", "verified"],
        );
    });
});

// Keys a fixture uses that the schema at that point does not declare (where
// it declares properties and closes the rest).
function undeclared(root: Schema, node: unknown, value: unknown, at = ""): string[] {
    const s = deref(root, node);
    if (!s || value === null || typeof value !== "object") return [];
    if (Array.isArray(value))
        return value.flatMap((v, i) => undeclared(root, s.items, v, `${at}[${i}]`));
    const props = (s.properties ?? {}) as Schema;
    return Object.entries(value).flatMap(([k, v]) => {
        if (k in props) return undeclared(root, props[k], v, `${at}.${k}`);
        if (s.additionalProperties === false) return [`${at}.${k}`];
        return undeclared(root, s.additionalProperties, v, `${at}.${k}`);
    });
}

describe("doctor contract: the hand-written fixtures use declared keys", () => {
    test.each([
        ["status", "status.json"],
        ["triage", "triage-1000001.json"],
        ["fixes", "fixes-1000001/fixes.json"],
    ])("%s fixture %s", (s, file) => {
        expect(undeclared(SCHEMAS[s], SCHEMAS[s], load(file))).toEqual([]);
    });
});
