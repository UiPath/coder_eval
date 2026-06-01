import { describe, expect, test } from "vitest";
import type { MessageEvent, TokenTotals } from "../runs";
import {
    buildThinkingModel,
    projectThinking,
    resolvePricing,
    thinkingAmplification,
    toolAmplification,
} from "../thinkingSim";

// Minimal assistant emission. Only the fields the simulator reads matter;
// thinkingMs/generationMs drive the thinking-token estimate, outputTokens
// drives per-message mode + apportionment, and cacheWriteTokens (minus the
// prior call's output) drives the tool-result estimate.
function call(opts: {
    generationMs: number;
    thinkingMs: number;
    outputTokens: number;
    cacheWriteTokens?: number | null;
    thinkingOutputTokens?: number | null;
    model?: string;
}): MessageEvent {
    return {
        index: 0,
        role: "assistant",
        startedAt: null,
        completedAt: null,
        generationMs: opts.generationMs,
        thinkingMs: opts.thinkingMs,
        textMs: null,
        toolGenMs: null,
        blockTypes: [],
        thinkingText: null,
        text: null,
        toolUses: [],
        inputTokens: 0,
        outputTokens: opts.outputTokens,
        cacheWriteTokens:
            opts.cacheWriteTokens === undefined ? 0 : opts.cacheWriteTokens,
        cacheReadTokens: 0,
        reasoningTokens: null,
        thinkingOutputTokens:
            opts.thinkingOutputTokens === undefined
                ? null
                : opts.thinkingOutputTokens,
        textOutputTokens: null,
        model: opts.model ?? "claude-sonnet-4-6",
    };
}

const TOTALS: TokenTotals = {
    input: 10,
    output: 300,
    cacheCreation: 300,
    cacheRead: 1000,
    total: 1610,
};

// 3 calls of equal generation time; only call 0 (earliest) thinks. Thinking is
// estimated as total_output · (thinkingMs_0 / Σ gen) = 300 · (10/30) = 100.
function earlyThinkingModel() {
    const messages = [
        call({ generationMs: 10, thinkingMs: 10, outputTokens: 100 }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
    ];
    return buildThinkingModel(messages, TOTALS, 0.5);
}

describe("resolvePricing", () => {
    test("known canonical id", () => {
        expect(resolvePricing("claude-sonnet-4-6")?.outputPerMTok).toBe(15);
    });
    test("strips trailing date suffix", () => {
        expect(resolvePricing("claude-sonnet-4-6-20991231")?.outputPerMTok).toBe(15);
    });
    test("unknown model → null", () => {
        expect(resolvePricing("definitely-not-a-model")).toBeNull();
        expect(resolvePricing(null)).toBeNull();
    });
});

describe("buildThinkingModel — cascade coefficients", () => {
    test("early thinking is written once and re-read on every later call", () => {
        const m = earlyThinkingModel();
        expect(m).not.toBeNull();
        if (!m) return;
        expect(m.calls).toBe(3);
        expect(m.thinkTokens).toBeCloseTo(100, 5);
        // call 0 of 3: later=2 → 1 write + 1 read of its thinking.
        expect(m.coeffOutput).toBeCloseTo(100, 5);
        expect(m.coeffCacheWrite).toBeCloseTo(100, 5);
        expect(m.coeffCacheRead).toBeCloseTo(100, 5);
        expect(m.perMessageTokens).toBe(true);
    });

    test("late thinking has no downstream cache cascade", () => {
        // Same 100 thinking tokens, but on the LAST call → no later calls.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
            call({ generationMs: 10, thinkingMs: 10, outputTokens: 100 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        expect(m).not.toBeNull();
        if (!m) return;
        expect(m.coeffOutput).toBeCloseTo(100, 5);
        expect(m.coeffCacheWrite).toBeCloseTo(0, 5); // later=0
        expect(m.coeffCacheRead).toBeCloseTo(0, 5);
    });

    test("unknown model → null model", () => {
        const messages = [
            call({ generationMs: 10, thinkingMs: 10, outputTokens: 100, model: "mystery" }),
        ];
        expect(buildThinkingModel(messages, TOTALS, null)).toBeNull();
    });

    test("legacy run (no per-message output) apportions by generation time", () => {
        const messages = [
            call({ generationMs: 20, thinkingMs: 20, outputTokens: 0 }),
            call({ generationMs: 20, thinkingMs: 0, outputTokens: 0 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        expect(m).not.toBeNull();
        if (!m) return;
        expect(m.perMessageTokens).toBe(false);
        // total_output(300) · thinkingMs_0(20) / Σ gen(40) = 150.
        expect(m.thinkTokens).toBeCloseTo(150, 5);
    });
});

describe("projectThinking", () => {
    test("scale 1 reproduces the recorded token mix", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        const base = projectThinking(m, 1);
        expect(base.outputTokens).toBe(300);
        expect(base.cacheCreationTokens).toBe(300);
        expect(base.cacheReadTokens).toBe(1000);
        expect(base.inputTokens).toBe(10);
    });

    test("scale 0 removes thinking from output AND the cache cascade", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        const z = projectThinking(m, 0);
        expect(z.outputTokens).toBeCloseTo(200, 5); // 300 - 100
        expect(z.cacheCreationTokens).toBeCloseTo(200, 5); // 300 - 100
        expect(z.cacheReadTokens).toBeCloseTo(900, 5); // 1000 - 100
        expect(z.costUsd).toBeLessThan(projectThinking(m, 1).costUsd);
    });

    test("scale 2 doubles the thinking-attributable tokens", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        const d = projectThinking(m, 2);
        expect(d.outputTokens).toBeCloseTo(400, 5); // 300 + 100
        expect(d.cacheReadTokens).toBeCloseTo(1100, 5);
        expect(d.costUsd).toBeGreaterThan(projectThinking(m, 1).costUsd);
    });

    test("clamps negative token counts to zero", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        // Far below 0% is impossible via the slider, but the math must not
        // produce negative tokens.
        const z = projectThinking(m, -5);
        expect(z.outputTokens).toBeGreaterThanOrEqual(0);
        expect(z.cacheReadTokens).toBeGreaterThanOrEqual(0);
    });
});

describe("thinkingAmplification", () => {
    test("equals (out+write+read price) / out price for early thinking", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        // sonnet: (15 + 3.75 + 0.3) / 15 = 1.27
        expect(thinkingAmplification(m)).toBeCloseTo(19.05 / 15, 4);
    });

    test("null when the run has no thinking", () => {
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(thinkingAmplification(m)).toBeNull();
    });
});

// 3 calls, no thinking. Tool-result tokens are inferred from cache growth the
// model's own output doesn't explain: toolResult_j = cacheWrite_{j+1} − out_j.
//   call0: out=100                       (its own write is never used)
//   call1: write=300, out=50 → result_0 = 300 − 100 = 200
//   call2: write=150, out=50 → result_1 = 150 −  50 = 100
//   (call2 is last → no result measured)
// Σ result = 300 (= coeffToolWrite). Cascade reads: result_0 (later=2) re-read
// once → 200; result_1 (later=1) re-read zero times → 0. coeffToolRead = 200.
function toolModel() {
    const messages = [
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, cacheWriteTokens: 0 }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 150 }),
    ];
    return buildThinkingModel(messages, TOTALS, 0.5);
}

describe("buildThinkingModel — tool-result estimation", () => {
    test("infers tool-result volume from cache growth beyond output", () => {
        const m = toolModel();
        expect(m).not.toBeNull();
        if (!m) return;
        expect(m.toolResultTokens).toBeCloseTo(300, 5);
        expect(m.coeffToolWrite).toBeCloseTo(300, 5);
        expect(m.coeffToolRead).toBeCloseTo(200, 5);
        // No thinking in this run — the two dimensions are disjoint.
        expect(m.thinkTokens).toBeCloseTo(0, 5);
        expect(m.coeffOutput).toBeCloseTo(0, 5);
    });

    test("clamps when output exceeds the next call's cache write", () => {
        // call1 writes fewer tokens than call0 emitted → negative, clamp to 0.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 500, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 100 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(m.toolResultTokens).toBeCloseTo(0, 5);
    });

    test("no per-message cache write → tool lever disabled", () => {
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, cacheWriteTokens: null }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: null }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(m.toolResultTokens).toBe(0);
        expect(m.coeffToolWrite).toBe(0);
        expect(m.coeffToolRead).toBe(0);
    });
});

describe("projectThinking — tool-output lever", () => {
    test("tool scale moves the cache cascade but never output", () => {
        const m = toolModel();
        if (!m) return;
        const base = projectThinking(m, 1, 1);
        const z = projectThinking(m, 1, 0);
        const d = projectThinking(m, 1, 2);
        // Output is untouched by tool scaling (tool results are injected, not
        // generated) — this is the core thinking/tool distinction.
        expect(z.outputTokens).toBe(base.outputTokens);
        expect(d.outputTokens).toBe(base.outputTokens);
        // Cache write/read scale with the tool lever.
        expect(z.cacheCreationTokens).toBeCloseTo(0, 5); // 300 − 300
        expect(z.cacheReadTokens).toBeCloseTo(800, 5); // 1000 − 200
        expect(d.cacheCreationTokens).toBeCloseTo(600, 5); // 300 + 300
        expect(d.cacheReadTokens).toBeCloseTo(1200, 5); // 1000 + 200
        expect(z.costUsd).toBeLessThan(base.costUsd);
        expect(d.costUsd).toBeGreaterThan(base.costUsd);
    });

    test("thinking and tool levers compose independently (deltas add)", () => {
        const messages = [
            call({ generationMs: 10, thinkingMs: 10, outputTokens: 100, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 150 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        // call0 thinking est = 300·(10/30) = 100. The tool estimate subtracts
        // that SAME 100 (not the recorded output) from call1's write, so the
        // thinking tokens don't leak into the tool population:
        //   result_0 = cw_1(300) − [think(100) + nonThink(100)] = 100
        //   result_1 = cw_2(150) − [think(0)   + nonThink(50)]  = 100
        // coeffToolWrite=200, coeffToolRead=100 (result_0 re-read once).
        expect(m.coeffToolWrite).toBeCloseTo(200, 5);
        expect(m.coeffToolRead).toBeCloseTo(100, 5);
        const both = projectThinking(m, 2, 2);
        // output: 300 + think(100) ; cacheCreation: 300 + think(100) + tool(200) ;
        // cacheRead: 1000 + think(100) + tool(100).
        expect(both.outputTokens).toBeCloseTo(400, 5);
        expect(both.cacheCreationTokens).toBeCloseTo(600, 5);
        expect(both.cacheReadTokens).toBeCloseTo(1200, 5);
    });

    test("under-recorded thinking does not leak into the tool estimate", () => {
        // A thinking-heavy call whose per-message output badly under-counts the
        // thinking block (10 logged for a long think). The gen-time estimate
        // says it generated ~all of OUT; the tool estimate must subtract THAT,
        // not the recorded 10, or the missing thinking is misread as a tool
        // result and double-counted by both levers.
        const totals: TokenTotals = {
            input: 0,
            output: 1000,
            cacheCreation: 1000,
            cacheRead: 0,
            total: 2000,
        };
        const messages = [
            // gen 10ms is the only generation time → think est = 1000·(10/10).
            call({
                generationMs: 10,
                thinkingMs: 10,
                outputTokens: 10,
                thinkingOutputTokens: 10,
                cacheWriteTokens: 0,
            }),
            call({ generationMs: 0, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 1000 }),
        ];
        const m = buildThinkingModel(messages, totals, null);
        if (!m) return;
        expect(m.thinkTokens).toBeCloseTo(1000, 5);
        // cw_1(1000) − [think(1000) + nonThink(0)] = 0 → no phantom tool result.
        expect(m.toolResultTokens).toBeCloseTo(0, 5);
        // The buggy form (cw_1 − recorded_out 10 = 990) would have attributed
        // nearly the whole write to tool results.
    });
});

describe("toolAmplification", () => {
    test("lifetime cache cost as a multiple of one cache write", () => {
        const m = toolModel();
        if (!m) return;
        // sonnet cacheWrite=3.75, cacheRead=0.3:
        // (300·3.75 + 200·0.3) / (300·3.75) = 1185 / 1125.
        expect(toolAmplification(m)).toBeCloseTo(1185 / 1125, 4);
    });

    test("null when the run injected no measurable tool results", () => {
        const m = earlyThinkingModel(); // thinking only, no tool results
        if (!m) return;
        expect(toolAmplification(m)).toBeNull();
    });
});
