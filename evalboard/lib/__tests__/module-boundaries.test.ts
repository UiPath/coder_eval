import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";

// Two bundle-correctness invariants that are otherwise enforced only by
// comments, each worth a measured ~105 kB of First Load JS. Both are violated by
// adding one import, and neither shows up in tsc, the test suite, or a passing
// `next build` — only in the bundle size, which nobody reads on a green PR.

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

function readSource(rel: string): string {
    return readFileSync(resolve(root, rel), "utf8");
}

// Matches `import … from "x"`, `import "x"`, and `export … from "x"`.
function moduleSpecifiers(src: string): string[] {
    return [...src.matchAll(/(?:^|\n)\s*(?:import|export)[\s\S]*?from\s+"([^"]+)"/g)]
        .map((m) => m[1])
        .concat(
            [...src.matchAll(/(?:^|\n)\s*import\s+"([^"]+)"/g)].map((m) => m[1]),
        );
}

describe("lib/harness.ts is a true leaf", () => {
    // Client components ("use client" charts, the selector, the badge) all import
    // this module for values, not just types. lib/overview.ts transitively reaches
    // node:fs, node:path and @azure/storage-blob, so a single VALUE import from
    // there would drag that whole graph into the browser bundle. Type-only
    // imports erase at compile time; values do not.
    //
    // This is why the module's own header says it must stay dependency-free, and
    // why anything that needs both harness knowledge and run data (the chart
    // pivot in app/_overview/harness-series.ts) imports harness.ts rather than
    // the other way round.
    test("has no imports at all", () => {
        const specs = moduleSpecifiers(readSource("lib/harness.ts"));
        expect(
            specs,
            "lib/harness.ts must stay dependency-free — see the leaf-module note at its top",
        ).toEqual([]);
    });
});

describe("the tag rail stays chart-free", () => {
    // tag-rail.tsx (ChipLegend / MergedTagRail) renders on pages with no chart at
    // all — /runs/[id] via run-view and /trends via trends-view — as well as on
    // the overview. Pulling chart code in here measured +105 kB First Load JS on
    // /runs/[id] (163 -> 268 kB) and on /trends (119 -> 225 kB).
    //
    // The tempting regression is to reuse the overview's swatch/legend
    // primitives: app/_overview/harness-legend.tsx is a sibling that looks
    // reusable, but it is chart-side and pulls the chart module's types and, via
    // the charts, recharts itself. Duplicating a 6-line swatch is the cheaper
    // trade.
    const FORBIDDEN = ["recharts", "harness-series", "harness-legend"];

    test("imports neither recharts nor any chart module", () => {
        const specs = moduleSpecifiers(readSource("app/_overview/tag-rail.tsx"));
        for (const spec of specs) {
            for (const bad of FORBIDDEN) {
                expect(
                    spec.includes(bad),
                    `tag-rail.tsx must not import ${spec} — it renders on chart-less pages`,
                ).toBe(false);
            }
        }
    });

    test("its dependencies stay on a short, reviewed list", () => {
        // Keeps the guard above honest: a blocklist only catches the imports we
        // thought of, so any NEW dependency has to be added here deliberately —
        // at which point that dependency's own graph gets re-checked.
        expect(
            moduleSpecifiers(readSource("app/_overview/tag-rail.tsx")).sort(),
        ).toEqual(["@/lib/overview", "next/link"]);
    });
});
