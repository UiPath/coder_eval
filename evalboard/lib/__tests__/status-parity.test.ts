import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { statusCategory, type StatusCategory } from "../status";

// Drift guard: lib/status.ts is a hand-copied mirror of coder_eval's
// `FinalStatus.category` (src/coder_eval/models/enums.py). Python's side is
// guarded there by `assert set(_STATUS_CATEGORIES) == set(FinalStatus)`; this
// mirror was not, which is how NOT_GRADED came to be categorized as "unknown"
// and then counted as a failure by every rate helper in the app.
//
// status.test.ts covers the mapping, but it iterates a HAND-MAINTAINED record —
// so it can never fail when Python adds a tenth member. This file parses the
// Python table instead, following the lib/__tests__/pricing-parity.test.ts
// precedent, so the next status added upstream breaks the build here rather
// than silently rendering as grey "unknown" and inflating a denominator.

const here = dirname(fileURLToPath(import.meta.url));
const PY_PATH = resolve(here, "../../../src/coder_eval/models/enums.py");

// Match: `FinalStatus.SUCCESS: "succeeded",` inside _STATUS_CATEGORIES.
const ROW_RE = /FinalStatus\.([A-Z_]+):\s*"(succeeded|failed|error|ungraded)"/g;

// Python's four buckets -> the TS union. The names differ on ONE member
// ("succeeded" vs "passed"), deliberately: "passed" is the word the UI uses.
// Spelled out here so a rename on either side is a failure, not a silent
// fall-through to "unknown".
const PY_TO_TS: Record<string, StatusCategory> = {
    succeeded: "passed",
    failed: "failed",
    error: "error",
    ungraded: "ungraded",
};

function parsePythonCategories(): Record<string, StatusCategory> {
    const src = readFileSync(PY_PATH, "utf8");
    const start = src.indexOf("_STATUS_CATEGORIES");
    expect(start, "_STATUS_CATEGORIES not found in enums.py").toBeGreaterThan(
        -1,
    );
    // Bound the scan to that dict so _EXECUTION_FACT_STATUSES and the docstrings
    // below it cannot contribute phantom rows.
    const end = src.indexOf("\n}", start);
    const table = src.slice(start, end === -1 ? undefined : end);

    const out: Record<string, StatusCategory> = {};
    for (const m of table.matchAll(ROW_RE)) {
        out[m[1]] = PY_TO_TS[m[2]];
    }
    return out;
}

describe("status.ts mirrors coder_eval FinalStatus.category", () => {
    const py = parsePythonCategories();

    test("the Python table was actually parsed", () => {
        // A regex that silently matches nothing would make every assertion
        // below vacuous — the exact failure mode this file exists to prevent.
        expect(Object.keys(py).length).toBeGreaterThanOrEqual(9);
        expect(py.SUCCESS).toBe("passed");
        expect(py.NOT_GRADED).toBe("ungraded");
    });

    test.each(Object.entries(parsePythonCategories()))(
        "%s categorizes as %s on both sides",
        (status, expected) => {
            expect(statusCategory(status)).toBe(expected);
        },
    );

    test("no member falls through to unknown", () => {
        for (const status of Object.keys(py)) {
            expect(statusCategory(status)).not.toBe("unknown");
        }
    });
});
