import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { PRICING } from "../pricing";

// Drift guard: lib/pricing.ts is a hand-copied mirror of the authoritative
// Python table in src/coder_eval/pricing.py. If the backend reprices a
// model (or adds one) and this mirror isn't updated, the frontend's "estimated"
// USD figures silently disagree with the backend's authoritative Cost on the
// same tokens. This test parses the Python table and asserts both tables have
// the same model ids and the same four per-MTok rates — turning silent drift
// into a failing build.

const here = dirname(fileURLToPath(import.meta.url));
const PY_PATH = resolve(here, "../../../src/coder_eval/pricing.py");

// Match: "model-id": ModelPricing(1.25, 10.0, 1.25, 0.125),
const ROW_RE =
    /"([^"]+)":\s*ModelPricing\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)/g;

function parsePythonTable(): Record<
    string,
    [number, number, number, number]
> {
    const src = readFileSync(PY_PATH, "utf8");
    const out: Record<string, [number, number, number, number]> = {};
    for (const m of src.matchAll(ROW_RE)) {
        out[m[1]] = [
            Number(m[2]),
            Number(m[3]),
            Number(m[4]),
            Number(m[5]),
        ];
    }
    return out;
}

describe("pricing.ts ↔ pricing.py parity", () => {
    const py = parsePythonTable();

    test("parses a non-trivial Python table", () => {
        // Guard against a regex/path regression silently passing the test.
        expect(Object.keys(py).length).toBeGreaterThan(10);
    });

    test("both tables list the same model ids", () => {
        expect(Object.keys(PRICING).sort()).toEqual(Object.keys(py).sort());
    });

    test("every model has identical input/output/cacheWrite/cacheRead rates", () => {
        for (const [model, [input, output, cw, cr]] of Object.entries(py)) {
            const ts = PRICING[model];
            expect(ts, `missing in lib/pricing.ts: ${model}`).toBeDefined();
            expect([
                ts.inputPerMTok,
                ts.outputPerMTok,
                ts.cacheWritePerMTok,
                ts.cacheReadPerMTok,
            ]).toEqual([input, output, cw, cr]);
        }
    });
});
