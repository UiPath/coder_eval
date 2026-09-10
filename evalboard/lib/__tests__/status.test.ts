import { describe, expect, it } from "vitest";

import {
    isGraded,
    isPassStatus,
    statusCategory,
    statusSortRank,
    type StatusCategory,
} from "../status";

// This module mirrors coder_eval's FinalStatus.category (models/enums.py), which
// is guarded there by `assert set(_STATUS_CATEGORIES) == set(FinalStatus)`. The
// mirror had no test at all, which is how NOT_GRADED came to be categorized as
// "unknown" and then counted as a failure by every rate helper downstream.
const EVERY_FINAL_STATUS: Record<string, StatusCategory> = {
    SUCCESS: "passed",
    FAILURE: "failed",
    ERROR: "error",
    BUILD_FAILED: "error",
    TIMEOUT: "failed",
    MAX_TURNS_EXHAUSTED: "failed",
    TOKEN_BUDGET_EXCEEDED: "failed",
    COST_BUDGET_EXCEEDED: "failed",
    NOT_GRADED: "ungraded",
};

describe("statusCategory", () => {
    it.each(Object.entries(EVERY_FINAL_STATUS))(
        "maps %s to %s",
        (status, expected) => {
            expect(statusCategory(status)).toBe(expected);
        },
    );

    it("treats a missing status as unknown, distinct from ungraded", () => {
        expect(statusCategory(null)).toBe("unknown");
        expect(statusCategory(null)).not.toBe(statusCategory("NOT_GRADED"));
    });

    it("does not classify an ungraded row as a pass or a failure", () => {
        // The whole point of the fourth category: folding it into either side
        // of a rate misreports a run that was never scored.
        expect(statusCategory("NOT_GRADED")).not.toBe("passed");
        expect(statusCategory("NOT_GRADED")).not.toBe("failed");
        expect(isPassStatus("NOT_GRADED")).toBe(false);
    });
});

describe("isGraded", () => {
    it("is false only for an ungraded row", () => {
        expect(isGraded("NOT_GRADED")).toBe(false);
        for (const status of Object.keys(EVERY_FINAL_STATUS)) {
            if (status === "NOT_GRADED") continue;
            expect(isGraded(status)).toBe(true);
        }
        // A null status is "no row here", not "ran but unscored".
        expect(isGraded(null)).toBe(true);
    });
});

describe("statusSortRank", () => {
    it("sorts failures first, ungraded in the middle, passes last", () => {
        expect(statusSortRank("FAILURE")).toBeLessThan(
            statusSortRank("NOT_GRADED"),
        );
        expect(statusSortRank("NOT_GRADED")).toBeLessThan(
            statusSortRank("SUCCESS"),
        );
    });
});
