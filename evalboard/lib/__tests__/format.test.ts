import { describe, expect, test } from "vitest";
import { fmtRunTime } from "../format";

describe("fmtRunTime", () => {
    test("reformats a daily-pipeline run id into a readable timestamp", () => {
        expect(fmtRunTime("2026-05-27_17-28-52")).toBe("2026-05-27 · 17:28:52");
    });

    test("returns ad-hoc run ids verbatim (no bogus split on underscores)", () => {
        expect(fmtRunTime("codex_skills_full_v2")).toBe("codex_skills_full_v2");
        expect(fmtRunTime("codex_skills_full_20260528T015221Z")).toBe(
            "codex_skills_full_20260528T015221Z",
        );
    });

    test("returns ids with no underscore verbatim", () => {
        expect(fmtRunTime("baseline-run")).toBe("baseline-run");
    });

    test("does not reformat a near-miss that fails the strict pattern", () => {
        // Missing seconds — not a valid daily id, so left untouched.
        expect(fmtRunTime("2026-05-27_17-28")).toBe("2026-05-27_17-28");
    });
});
