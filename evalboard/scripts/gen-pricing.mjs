// Generates lib/pricing.generated.ts from the authoritative Python rate table
// (src/coder_eval/pricing.py), so per-MTok rates are written in exactly one
// file in one language. Run `pnpm gen:pricing` after editing pricing.py.
//
// Also the single home of the Python-table parser: lib/__tests__/pricing-parity.test.ts
// imports parsePythonTable from here rather than re-declaring the regex, so the
// generator and its drift guard can never disagree about what pricing.py says.
//
// That shared parser is why ROW_RE's coverage has to be checked structurally
// rather than trusted: it only matches rows written as a literal
// `ModelPricing(a, b, c, d)`, and a row it fails to match is missing from BOTH
// the generated table and the parity test's expectation — so deep-equal passes
// while the board silently renders "—" for that model's cost. readTable()
// therefore cross-checks the matched-row count against the number of
// `ModelPricing(` constructions in the file and refuses to emit on a mismatch.

import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Resolve both paths from the script's own location, never cwd, so the
// generator behaves identically from `evalboard/` or the repo root.
const here = dirname(fileURLToPath(import.meta.url));
const PY_PATH = resolve(here, "../../src/coder_eval/pricing.py");
const OUT_PATH = resolve(here, "../lib/pricing.generated.ts");

// Match: "model-id": ModelPricing(1.25, 10.0, 1.25, 0.125),
const ROW_RE =
    /"([^"]+)":\s*ModelPricing\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)/g;

// Every `ModelPricing(` in the file should be one matched table row.
const CONSTRUCTOR_RE = /ModelPricing\(/g;

// Below this, assume a regex or path regression rather than a genuinely tiny
// table — emitting an empty/near-empty table would silently unprice the board.
const MIN_ROWS = 10;

// Models priced in pricing.py that must NOT reach the frontend table.
//
// This is the ONE exception to "generate everything", and it is not a
// convenience allowlist — the old hand-maintained not-mirrored list is exactly
// what this generator exists to delete (17 models drifted past it, two of them
// models real runs use, silently rendering "—" for their cost). An entry here
// means a static rate would be WRONG, not merely unused.
//
// OpenRouter routes each request to whichever provider wins its sort, so the
// billed rate depends on where the call landed and no single headline rate is
// correct. pricing.py keeps these solely as the Python-side `max_usd` static
// fallback; the harness captures each call's ACTUAL cost proxy-side and the
// detail view renders it per call (TurnRecord.provider_call_costs →
// ProviderCallTableSection). Mirroring the headline rate here would put a
// confidently wrong number next to the measured one.
//
// Bedrock open-weight models (deepseek.v3.2, zai.glm-5, moonshotai.kimi-k2.5)
// are deliberately NOT excluded: they run at fixed Bedrock rates like Claude,
// with no per-call capture, so static pricing is correct and required.
const EXCLUDED_MODELS = new Set([
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-pro",
]);

function readSource() {
    try {
        return readFileSync(PY_PATH, "utf8");
    } catch (err) {
        throw new Error(
            `cannot read the Python pricing table at ${PY_PATH}: ${err instanceof Error ? err.message : err}`,
        );
    }
}

/**
 * Parse the Python rate table into `{ model: [input, output, cacheWrite, cacheRead] }`,
 * preserving pricing.py's insertion order.
 *
 * Uses a null-prototype object so a degenerate key (`__proto__`) becomes an own
 * property instead of silently setting the prototype and dropping the row.
 * @returns {Record<string, [number, number, number, number]>}
 */
export function parsePythonTable() {
    const src = readSource();
    /** @type {Record<string, [number, number, number, number]>} */
    const out = Object.create(null);
    for (const m of src.matchAll(ROW_RE)) {
        const rates = [Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5])];
        if (!rates.every(Number.isFinite)) {
            throw new Error(
                `non-finite rate for "${m[1]}" in ${PY_PATH}: [${rates.join(", ")}]. ` +
                    `Emitting NaN would typecheck and render "$NaN" on the board.`,
            );
        }
        out[m[1]] = /** @type {[number, number, number, number]} */ (rates);
    }
    return out;
}

/**
 * Parse, then verify the parse actually covered the file.
 *
 * The drift guard MUST use this rather than parsePythonTable: comparing two
 * products of the same parser cannot detect a row the parser skipped, because
 * the row is missing from the artifact AND from the expectation. This is the
 * only function that can tell "the mirror is faithful" apart from "the mirror
 * and the expectation are equally blind".
 * @returns {Record<string, [number, number, number, number]>}
 */
export function readTable() {
    const src = readSource();
    const table = parsePythonTable();
    const rows = Object.keys(table).length;
    if (rows < MIN_ROWS) {
        throw new Error(
            `parsed only ${rows} rows from ${PY_PATH} (expected at least ${MIN_ROWS}). ` +
                `The regex or the path has regressed — refusing to emit a partial table.`,
        );
    }
    const constructions = (src.match(CONSTRUCTOR_RE) ?? []).length;
    if (constructions !== rows) {
        throw new Error(
            `${PY_PATH} has ${constructions} ModelPricing(...) constructions but ROW_RE matched ${rows} rows.\n` +
                `A rate row ROW_RE cannot parse (keyword arguments, a variable, a computed expression, ` +
                `scientific notation) would be dropped from the generated table AND invisible to the parity ` +
                `test, silently unpricing that model. Either rewrite the row as a literal ` +
                `\`"model": ModelPricing(a, b, c, d)\`, or widen ROW_RE. ` +
                `(If a ModelPricing(...) legitimately lives outside the table, teach this check to skip it.)`,
        );
    }
    return table;
}

/**
 * The subset of pricing.py that the frontend table mirrors: everything except
 * EXCLUDED_MODELS. This is what both the generator emits and the drift guard
 * compares against, so the exclusion is stated once.
 *
 * Throws on a STALE exclusion — an id listed in EXCLUDED_MODELS that pricing.py
 * no longer prices. Without this an exclusion outlives the model it was written
 * for, and if that id is ever reintroduced (as a normal fixed-rate model) it
 * would be silently dropped from the board, which is the very failure the
 * hand-maintained allowlist kept producing.
 * @param {Record<string, [number, number, number, number]>} table
 * @returns {Record<string, [number, number, number, number]>}
 */
export function mirroredTable(table) {
    const stale = [...EXCLUDED_MODELS].filter((m) => !(m in table));
    if (stale.length > 0) {
        throw new Error(
            `EXCLUDED_MODELS lists ${stale.join(", ")}, which ${PY_PATH} no longer prices. ` +
                `Drop the stale entr${stale.length === 1 ? "y" : "ies"} — an exclusion that outlives its model ` +
                `silently unprices that id if it is ever reintroduced.`,
        );
    }
    return Object.fromEntries(
        Object.entries(table).filter(([model]) => !EXCLUDED_MODELS.has(model)),
    );
}

/** The ids deliberately withheld from the frontend table. See EXCLUDED_MODELS. */
export function excludedModels() {
    return [...EXCLUDED_MODELS];
}

/**
 * Render the generated TypeScript module. Every key is quoted unconditionally
 * via JSON.stringify — `z-ai/glm-5.2`, `deepseek.v3.2`, and `gpt-5.1-codex-max`
 * all need it, and deciding per key costs more than it saves. Rates are
 * interpolated as-is, so a legitimate `0` (Bedrock open-weight cache-read) is
 * emitted as `0`; parsePythonTable has already guaranteed each is finite.
 * @param {Record<string, [number, number, number, number]>} table
 * @returns {string}
 */
function renderModule(table) {
    const rows = Object.entries(table)
        .map(
            ([model, [input, output, cacheWrite, cacheRead]]) =>
                `    ${JSON.stringify(model)}: {\n` +
                `        inputPerMTok: ${input},\n` +
                `        outputPerMTok: ${output},\n` +
                `        cacheWritePerMTok: ${cacheWrite},\n` +
                `        cacheReadPerMTok: ${cacheRead},\n` +
                `    },`,
        )
        .join("\n");
    return (
        `// GENERATED BY scripts/gen-pricing.mjs — DO NOT EDIT.\n` +
        `// Source of truth: src/coder_eval/pricing.py.\n` +
        `// To change a rate: edit that file, then run \`pnpm gen:pricing\`.\n` +
        `// Row order mirrors pricing.py so regeneration diffs stay minimal.\n` +
        `//\n` +
        `// Not every pricing.py model appears here: the OpenRouter open-weight ids are\n` +
        `// withheld on purpose, because they are routed per-request and shown at their\n` +
        `// captured ACTUAL per-call cost instead. See EXCLUDED_MODELS in the generator.\n` +
        `\n` +
        `import type { Pricing } from "./pricing-types";\n` +
        `\n` +
        `export const GENERATED_PRICING: Record<string, Pricing> = {\n` +
        `${rows}\n` +
        `};\n`
    );
}

function main() {
    let table;
    try {
        table = mirroredTable(readTable());
    } catch (err) {
        // Every guard above throws rather than emitting, so a failed run leaves
        // the previous artifact intact instead of writing a partial table.
        console.error(
            `gen-pricing: ${err instanceof Error ? err.message : err}`,
        );
        process.exit(1);
    }
    writeFileSync(OUT_PATH, renderModule(table), "utf8");
    console.log(
        `gen-pricing: wrote ${Object.keys(table).length} models to ${OUT_PATH}`,
    );
}

// Only emit when run as a script; importers (the parity test) get the parser
// alone. realpathSync, not resolve: Node realpaths the main module for
// import.meta.url, so a lexical compare silently no-ops (exit 0, nothing
// written) whenever a symlink is anywhere in the invocation path.
if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
    main();
}
