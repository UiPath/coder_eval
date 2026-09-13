import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";

// The TypeScript counterpart to CE058.
//
// CE058 stops an unmeasured TIMING value becoming a numeric literal in `src/`:
// `durationMs == null` means *never timed* and `0` means *timed and instant*,
// so writing the literal publishes the second while meaning the first. The
// evalboard carries the same contract — `sumMeasured` (lib/runs.ts) implements
// it correctly, returning null when nothing was measured — but nothing stopped
// the next author writing `?? 0` where an unmeasured value must stay null.
//
// There is no eslint in evalboard/ (package.json has only typecheck / test /
// build), so this is a vitest source scan rather than a lint plugin.
//
// WHY AN ALLOWLIST RATHER THAN A BAN. The residual arithmetic in _sections.tsx
// uses `?? 0` *correctly*: it subtracts only what was measured, which is the
// whole point. A blanket ban fires on right code. So every occurrence must be
// listed with a reason, and a NEW one fails until its author either justifies
// it here or uses null.
//
// WHY IT KEYS ON TIMING NAMES, and this is a deliberate narrowing. A scan for
// every `?? 0` in these files matches 58 occurrences, roughly half of them
// token or cache buckets (`inputTokens ?? 0`, `prev?.input ?? 0`) where zero is
// a perfectly good answer — tokens are COUNTED, not measured, so there is no
// None-vs-0 ambiguity to protect. An allowlist that long, much of it saying
// "token bucket, fine", is one nobody reads and everybody appends to. CE058's
// contract is about TIMING, so this matches the codebase's own naming
// conventions for a measured interval: an identifier ending in `Ms`, `Seconds`,
// or `duration`/`Duration`.
//
// DECLARED BLIND SPOTS, stated the way the Python rules state theirs:
//   * only the three files below are covered; anything else gains no protection;
//   * a timing field named by NONE of those conventions is invisible — the
//     convention IS the rule, and it is a convention rather than a type;
//   * `?? 0.0`, `|| 0.0` and an `if (x == null) x = 0` assignment are not matched;
//   * comment stripping cuts from the FIRST `//` on a line, so a coalesce that
//     follows a URL or a `//` inside a string literal is invisible. Cheap to
//     hit only on purpose, and the alternative is parsing TypeScript.

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

const COVERED = [
    "lib/runs.ts",
    "lib/timing.ts",
    "app/runs/[id]/[...task]/_sections.tsx",
];

// Keyed on the trimmed source LINE, not a line number: numbers move on every
// edit and the test would fail for unrelated reasons.
const ALLOWED = new Map<string, string>([
    [
        "? executed.reduce((a, t) => a + (t.duration ?? 0), 0)",
        "Guarded: the enclosing branch only runs when `allHaveDuration` is true, so every term was measured.",
    ],
    [
        "const totalGenMs = mainThread.reduce((s, m) => s + (m.generationMs ?? 0), 0);",
        "Summing a breakdown: an unmeasured emission contributes nothing to the total, which is what a sum of the measured means.",
    ],
    [
        "const thinkingMs = mainThread.reduce((s, m) => s + (m.thinkingMs ?? 0), 0);",
        "Summing a breakdown: an unmeasured emission contributes nothing, which is what a sum of the measured means.",
    ],
    [
        "const textMs = mainThread.reduce((s, m) => s + (m.textMs ?? 0), 0);",
        "Summing a breakdown: an emission with no text time contributes nothing to the text total.",
    ],
    [
        "const toolGenMs = mainThread.reduce((s, m) => s + (m.toolGenMs ?? 0), 0);",
        "Summing a breakdown: an emission with no tool-gen time contributes nothing to that total.",
    ],
    [
        "const mixedMs = mainThread.reduce((s, m) => s + (m.mixedGenMs ?? 0), 0);",
        "Summing a breakdown: an emission with no mixed-block time contributes nothing to that total.",
    ],
    [
        "(m) => (m.generationMs ?? 0) >= SLOW_GEN_MS,",
        "A threshold comparison: an unmeasured generation is not a slow one, and 0 is the right answer to the question asked.",
    ],
    [
        "s + m.toolUses.filter((t) => (t.durationMs ?? 0) >= SLOW_TOOL_MS).length,",
        "A threshold comparison: an untimed call is not a slow call, so 0 answers the question asked.",
    ],
    [
        "(toolExecMs ?? 0) -",
        "The residual's tool half, and the reason the cell itself now renders a dash: a turn with no BOUNDED span measured no tool time, so its time belongs IN the residual rather than being subtracted as a zero.",
    ],
    [
        "(harnessStartupMs ?? 0) -",
        "The residual. Subtracting only what was measured is the whole point; an unmeasured head leaves its time IN the residual rather than silently claiming it.",
    ],
    [
        "(harnessTeardownMs ?? 0) -",
        "The residual's tail half: subtracting only what was measured leaves unmeasured time IN the residual.",
    ],
    [
        "(setupMs ?? 0) -",
        "Same residual rule: a run predating the field leaves its setup time IN the residual rather than having it silently subtracted as zero.",
    ],
    [
        "(gradingMs ?? 0)",
        "Same residual rule, and it is also the ungraded case — `coder-eval execute` grades nothing, so there is no grading time to subtract.",
    ],
    [
        "const slowExec = (execMs ?? 0) >= SLOW_TOOL_MS;",
        "A threshold comparison: an untimed execution is not a slow one, so 0 answers the question asked.",
    ],
    [
        "const slowGen = (m.generationMs ?? 0) >= SLOW_GEN_MS;",
        "A threshold comparison: an unmeasured generation is not a slow one.",
    ],
    [
        "const slowTool = m.toolUses.some((t) => (t.durationMs ?? 0) >= SLOW_TOOL_MS);",
        "A threshold comparison: an untimed call is not a slow call.",
    ],
]);

// `x ?? 0` / `x || 0` where the coalesced identifier names a measured interval.
// Not anchored to the line start, so several on one line are all found.
const COALESCE = /\b[\w$]*(?:Ms|Seconds|[Dd]uration)\s*(?:\?\?|\|\|)\s*0(?![.\d\w])/g;

export function findCoalesces(source: string): string[] {
    const hits: string[] = [];
    for (const raw of source.split("\n")) {
        // Strip line comments so prose about `?? 0` is not a violation.
        const line = raw.replace(/\/\/.*$/, "");
        if (COALESCE.test(line)) hits.push(raw.trim());
        COALESCE.lastIndex = 0;
    }
    return hits;
}

describe("no unguarded zero-coalesce on a timing value", () => {
    for (const relative of COVERED) {
        test(relative, () => {
            const source = readFileSync(resolve(root, relative), "utf8");
            const unlisted = findCoalesces(source).filter((line) => !ALLOWED.has(line));
            expect(
                unlisted,
                `${relative} coalesces an unmeasured timing value to 0 without a reason. ` +
                    `\`x ?? 0\` on a \`…Ms\` field publishes "measured, and instant" where null means ` +
                    `"never measured" — the contract CE058 enforces on the Python side. If the zero is ` +
                    `correct here (a sum, or a threshold comparison), add the line to ALLOWED with a ` +
                    `one-line reason; otherwise keep it null.`,
            ).toEqual([]);
        });
    }

    test("the scanner actually matches something (a scan that finds nothing proves nothing)", () => {
        // The failure mode that makes a source scanner worthless: a regex that
        // silently stops matching, after which every file "passes".
        const found = COVERED.flatMap((relative) =>
            findCoalesces(readFileSync(resolve(root, relative), "utf8")),
        );
        expect(found.length).toBeGreaterThan(10);
    });

    test("every allowlist entry is still present in a covered file", () => {
        // An entry nobody needs is an entry that outlived its reason.
        const all = new Set(
            COVERED.flatMap((relative) =>
                findCoalesces(readFileSync(resolve(root, relative), "utf8")),
            ),
        );
        expect([...ALLOWED.keys()].filter((line) => !all.has(line))).toEqual([]);
    });

    test("every allowlist entry carries a non-empty reason", () => {
        expect([...ALLOWED.entries()].filter(([, why]) => why.trim().length < 20)).toEqual([]);
    });

    test("NEGATIVE CONTROL: an un-allowlisted occurrence is reported", () => {
        // Without this the suite could pass by matching nothing at all.
        const hits = findCoalesces("const x = someTimingMs ?? 0;\n");
        expect(hits).toEqual(["const x = someTimingMs ?? 0;"]);
        expect(ALLOWED.has(hits[0])).toBe(false);
    });

    test("NEGATIVE CONTROL: prose and non-timing fields are not matched", () => {
        expect(findCoalesces("// the residual uses `?? 0` deliberately\n")).toEqual([]);
        expect(findCoalesces("const n = inputTokens ?? 0;\n")).toEqual([]);
        expect(findCoalesces("const n = count ?? 0;\n")).toEqual([]);
        // The widened conventions, so a regression to `Ms`-only is caught here.
        expect(findCoalesces("const s = taskSeconds ?? 0;\n")).toEqual(["const s = taskSeconds ?? 0;"]);
        expect(findCoalesces("const d = t.duration ?? 0;\n")).toEqual(["const d = t.duration ?? 0;"]);
    });
});
