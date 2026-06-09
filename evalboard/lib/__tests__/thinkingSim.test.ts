import { describe, expect, test } from "vitest";
import type { MessageEvent, MessageToolUse, TokenTotals } from "../runs";
import {
    buildThinkingModel,
    computeSkipDelta,
    projectPerMessage,
    projectThinking,
    thinkingAmplification,
    toolAmplification,
} from "../thinkingSim";
import { resolvePricing } from "../pricing";

// Minimal MessageToolUse for skip-tool tests. `resultTokens` is the measured
// (content-derived) size of this tool's result — what now drives the tool-output
// lever (the old cache-growth inference is gone).
function toolUse(
    toolName: string,
    outputTokens: number,
    resultPreview: string | null = null,
    resultTokens = 0,
): MessageToolUse {
    return {
        toolName,
        toolUseId: null,
        summary: toolName,
        argText: null,
        description: null,
        genMs: null,
        durationMs: null,
        isError: false,
        resultPreview,
        outputTokens,
        resultTokens,
    };
}

// Minimal assistant emission. Only the fields the simulator reads matter;
// thinkingMs/generationMs drive the thinking-token estimate, outputTokens
// drives per-message mode + apportionment, and cacheWriteTokens (minus the
// prior call's output) drives the tool-result estimate.
// Unique, monotonically increasing message index — production assigns these via
// parseMessages (1-based ++order), and projectPerMessage keys its per-row impact
// map on them, so fixtures must not all collide on index 0.
let __callIndex = 0;
function call(opts: {
    generationMs: number;
    thinkingMs: number;
    outputTokens: number;
    cacheWriteTokens?: number | null;
    thinkingOutputTokens?: number | null;
    model?: string;
    toolUses?: MessageToolUse[];
    // null = main thread (default), string = sub-agent branch, undefined =
    // run didn't record branch info (legacy → simulator disabled).
    parentToolUseId?: string | null;
}): MessageEvent {
    return {
        index: ++__callIndex,
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
        toolUses: opts.toolUses ?? [],
        inputTokens: 0,
        outputTokens: opts.outputTokens,
        cacheWriteTokens:
            opts.cacheWriteTokens === undefined ? 0 : opts.cacheWriteTokens,
        cacheReadTokens: 0,
        parentToolUseId:
            "parentToolUseId" in opts ? opts.parentToolUseId : null,
        reasoningTokens: null,
        thinkingOutputTokens:
            opts.thinkingOutputTokens === undefined
                ? null
                : opts.thinkingOutputTokens,
        textOutputTokens: null,
        model: opts.model ?? "claude-sonnet-4-6",
        costUsd: null,
        note: null,
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

// 3 calls, no thinking. Tool-result size is MEASURED from each tool's recorded
// result content (resultTokens) — cache-independent, no cache-growth inference:
//   call0: tool result 200, later=2 → write 200, read 200·(2-1)=200
//   call1: tool result 100, later=1 → write 100, read 100·0=0
//   call2: last call, no tool
// coeffToolWrite=300, coeffToolRead=200.
function toolModel() {
    const messages = [
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, toolUses: [toolUse("Bash", 0, null, 200)] }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, toolUses: [toolUse("Read", 0, null, 100)] }),
        call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
    ];
    return buildThinkingModel(messages, TOTALS, 0.5);
}

describe("buildThinkingModel — tool-result sizing (content-based)", () => {
    test("measures tool-result volume from recorded result content", () => {
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

    test("no recorded tool-result tokens → lever has nothing to size", () => {
        // Tools present but no result content recorded (resultTokens 0) → 0.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, toolUses: [toolUse("Bash", 5)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(m.toolResultTokens).toBe(0);
        expect(m.coeffToolWrite).toBe(0);
        expect(m.coeffToolRead).toBe(0);
    });

    test("is independent of per-message cache write (works the same with caching off)", () => {
        // Same measured result sizes, but per-message cacheWriteTokens left at 0
        // (as on a no-cache run). The content-based sizing is unchanged — the old
        // cache-growth inference would have produced 0 here.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, cacheWriteTokens: 0, toolUses: [toolUse("Bash", 0, null, 200)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 0, toolUses: [toolUse("Read", 0, null, 100)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, 0.5);
        if (!m) return;
        expect(m.coeffToolWrite).toBeCloseTo(300, 5);
        expect(m.coeffToolRead).toBeCloseTo(200, 5);
    });

    test("parallel tools in one call sum their measured result tokens", () => {
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 50,
                toolUses: [toolUse("Bash", 0, null, 100), toolUse("Read", 0, null, 50)],
            }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 20 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        // call0 result total = 150, later=1 → write 150, read 150·0 = 0.
        expect(m.coeffToolWrite).toBeCloseTo(150, 5);
        expect(m.coeffToolRead).toBeCloseTo(0, 5);
    });
});

describe("projectThinking — tool-output lever", () => {
    test("tool scale moves the cache cascade but never output (caching on)", () => {
        const m = toolModel();
        if (!m) return;
        const base = projectThinking(m, 1, 1);
        const z = projectThinking(m, 1, 0);
        const d = projectThinking(m, 1, 2);
        // Output is untouched by tool scaling (tool results are injected, not
        // generated) — this is the core thinking/tool distinction.
        expect(z.outputTokens).toBe(base.outputTokens);
        expect(d.outputTokens).toBe(base.outputTokens);
        // Cache write/read scale with the tool lever (cacheActive run).
        expect(z.cacheCreationTokens).toBeCloseTo(0, 5); // 300 − 300
        expect(z.cacheReadTokens).toBeCloseTo(800, 5); // 1000 − 200
        expect(d.cacheCreationTokens).toBeCloseTo(600, 5); // 300 + 300
        expect(d.cacheReadTokens).toBeCloseTo(1200, 5); // 1000 + 200
        expect(z.costUsd).toBeLessThan(base.costUsd);
        expect(d.costUsd).toBeGreaterThan(base.costUsd);
    });

    test("with caching OFF the tool lever moves INPUT (cascade re-sent as input)", () => {
        // Same measured results, but a no-cache run (cache buckets 0): the
        // tool-result cascade is re-sent as uncached input each call, so the
        // lever moves inputTokens, not cache — and cache stays 0.
        const totalsNoCache: TokenTotals = { input: 5000, output: 200, cacheCreation: 0, cacheRead: 0, total: 5200 };
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, toolUses: [toolUse("Bash", 0, null, 200)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, toolUses: [toolUse("Read", 0, null, 100)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, totalsNoCache, null);
        if (!m) return;
        expect(m.hasCacheWrite).toBe(false);
        expect(m.hasCacheRead).toBe(false);
        const base = projectThinking(m, 1, 1);
        const z = projectThinking(m, 1, 0);
        const d = projectThinking(m, 1, 2);
        // Tool result presence = write(300) + read(200) = 500 input tokens.
        expect(z.inputTokens).toBeCloseTo(5000 - 500, 5);
        expect(d.inputTokens).toBeCloseTo(5000 + 500, 5);
        // Cache buckets stay 0 in all positions; output untouched.
        expect(z.cacheCreationTokens).toBe(0);
        expect(d.cacheReadTokens).toBe(0);
        expect(z.outputTokens).toBe(base.outputTokens);
        expect(z.costUsd).toBeLessThan(base.costUsd);
        expect(d.costUsd).toBeGreaterThan(base.costUsd);
    });

    test("caching ON: over-reducing a tool result past the cache-write bucket spills into input", () => {
        // The cache-write bucket the run actually billed (300) is SMALLER than the
        // measured tool-result write portion (500). Skipping that result (toolScale=0)
        // must drain cache-write to exactly 0 and SPILL the 200-token remainder into
        // uncached input — not silently clamp it away (which would understate cost and
        // break the "drain first, spill the leftover" semantics).
        const totals: TokenTotals = { input: 1000, output: 150, cacheCreation: 300, cacheRead: 1000, total: 2450 };
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, toolUses: [toolUse("Bash", 0, null, 500)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, totals, null);
        if (!m) return;
        expect(m.hasCacheWrite).toBe(true);
        expect(m.coeffToolWrite).toBeCloseTo(500, 5); // single result, one trailing call → all write, no read
        expect(m.coeffToolRead).toBeCloseTo(0, 5);
        // Base projection reproduces the recorded mix exactly.
        const base = projectThinking(m, 1, 1);
        expect(base.cacheCreationTokens).toBeCloseTo(300, 5);
        expect(base.inputTokens).toBeCloseTo(1000, 5);
        // Skip the tool result entirely: write-portion delta = −500 against a 300 bucket.
        const z = projectThinking(m, 1, 0);
        expect(z.cacheCreationTokens).toBeCloseTo(0, 5); // drained, not negative
        expect(z.inputTokens).toBeCloseTo(800, 5); // 1000 + (300 − 500) spilled remainder
        expect(z.cacheReadTokens).toBeCloseTo(1000, 5); // read portion is 0 → untouched
        // The 200 spilled tokens are conserved (a naive clamp would have lost them).
        expect(z.totalTokens).toBeCloseTo(800 + 150 + 0 + 1000, 5);
        expect(z.costUsd).toBeLessThan(base.costUsd);
    });

    test("thinking and tool levers compose independently (deltas add)", () => {
        const messages = [
            call({ generationMs: 10, thinkingMs: 10, outputTokens: 100, toolUses: [toolUse("Bash", 0, null, 100)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, toolUses: [toolUse("Read", 0, null, 100)] }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        // call0 thinking est = 300·(10/30) = 100; later=2 → think write/read 100/100.
        // tool results: call0=100 (later2 → w100 r100), call1=100 (later1 → w100 r0)
        //   → coeffToolWrite=200, coeffToolRead=100.
        expect(m.coeffToolWrite).toBeCloseTo(200, 5);
        expect(m.coeffToolRead).toBeCloseTo(100, 5);
        const both = projectThinking(m, 2, 2);
        // output: 300 + think(100) ; cacheCreation: 300 + think(100) + tool(200) ;
        // cacheRead: 1000 + think(100) + tool(100).
        expect(both.outputTokens).toBeCloseTo(400, 5);
        expect(both.cacheCreationTokens).toBeCloseTo(600, 5);
        expect(both.cacheReadTokens).toBeCloseTo(1200, 5);
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

describe("buildThinkingModel — tree-structured (sub-agent) cascade", () => {
    // call 0 thinks 100 tokens inside sub-agent branch "T1" (calls 0,1,2),
    // followed by 7 main-thread calls. Only call 0 has generation time, so
    // thinkPerCall[0] = output_total · (10/10) = 100, the rest 0.
    function branched(parentOfFirstThree: string | null) {
        const sub = (extra: { thinkingMs?: number; generationMs?: number }) =>
            call({
                generationMs: extra.generationMs ?? 0,
                thinkingMs: extra.thinkingMs ?? 0,
                outputTokens: 0,
                parentToolUseId: parentOfFirstThree,
            });
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 10,
                outputTokens: 100,
                parentToolUseId: parentOfFirstThree,
            }),
            sub({}),
            sub({}),
            ...Array.from({ length: 7 }, () =>
                call({ generationMs: 0, thinkingMs: 0, outputTokens: 0 }),
            ),
        ];
        return buildThinkingModel(
            messages,
            { input: 0, output: 100, cacheCreation: 0, cacheRead: 0, total: 100 },
            null,
        );
    }

    test("sub-agent thinking cascades only within its branch, not the whole run", () => {
        const tree = branched("T1"); // calls 0,1,2 are a sub-agent branch
        expect(tree).not.toBeNull();
        if (!tree) return;
        expect(tree.thinkTokens).toBeCloseTo(100, 5);
        // Branch T1 = calls [0,1,2]; call 0 has 2 later same-branch calls →
        // 1 cache write + 1 cache read. The 7 trailing MAIN calls never re-read
        // the sub-agent's thinking.
        expect(tree.coeffCacheWrite).toBeCloseTo(100, 5);
        expect(tree.coeffCacheRead).toBeCloseTo(100, 5);
    });

    test("the flat-list model would have massively overcounted the same run", () => {
        // Identical calls, but all on the main thread (no branch) → call 0 sees
        // all 9 later calls: 1 write + 8 reads. This is the bug the tree fixes:
        // 800 phantom cache-read tokens vs the correct 100.
        const flat = branched(null);
        if (!flat) return;
        expect(flat.coeffCacheRead).toBeCloseTo(800, 5);
        const tree = branched("T1");
        if (!tree) return;
        expect(tree.coeffCacheRead).toBeLessThan(flat.coeffCacheRead);
    });

    test("a sub-agent spawn's fresh context write is not counted as a tool result", () => {
        // call 1 (sub-agent T1) opens with a 13k context cache-write. The flat
        // model misreads cacheWrite_{j+1} − output_j as a giant tool result of
        // call 0; the branch check excludes a cross-branch successor.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 0 }), // main
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 0,
                cacheWriteTokens: 13000,
                parentToolUseId: "T1", // sub-agent spawn (different branch)
            }),
        ];
        const m = buildThinkingModel(
            messages,
            { input: 0, output: 0, cacheCreation: 13000, cacheRead: 0, total: 13000 },
            null,
        );
        if (!m) return;
        expect(m.toolResultTokens).toBe(0);
        expect(m.coeffToolWrite).toBe(0);
    });

    test("legacy run without branch info → no model (simulator hidden)", () => {
        const m = buildThinkingModel(
            [
                call({
                    generationMs: 10,
                    thinkingMs: 10,
                    outputTokens: 100,
                    parentToolUseId: undefined, // run never recorded the field
                }),
            ],
            TOTALS,
            0.5,
        );
        expect(m).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// Skip-tool feature
//
// Setup: 3 calls, no thinking, with toolUses so we can test per-tool attribution.
//
//   call0: out=50, cw=0, tools=[Bash(gen=30, preview=10chars), Read(gen=15, preview=5chars)]
//   call1: out=20, cw=200, tools=[TaskCreate(gen=5, preview=2chars)]
//   call2: out=10, cw=100, tools=[]  (last call, no result measured)
//
// toolPerCall estimation (thinkPerCall=0 everywhere since no thinking):
//   j=0: next=call1 cw=200>0, same branch
//        k=0: cw=0→add(0+max(0,50-0)=50)→groupRealOutput=50; k=-1 stop
//        toolPerCall[0] = max(0, 200-50) = 150
//   j=1: next=call2 cw=100>0, same branch
//        k=1: cw=200>0→add(0+max(0,20-0)=20)→groupRealOutput=20; break
//        toolPerCall[1] = max(0, 100-20) = 80
//   j=2: last call → toolPerCall[2] = 0
//
// laterInBranch: call0→2, call1→1, call2→0
//
// cascade:
//   coeffToolWrite = 150 + 80 = 230
//   coeffToolRead  = 150*(2-1) + 80*(1-1) = 150
//
// turnProfiles[0] (call0, tools=[Bash,Read]):
//   toolResultCacheWrite = 150, toolResultCacheRead = 150*(2-1) = 150
//   Bash  preview=10, Read preview=5 → fractions: Bash=10/15, Read=5/15
//   Bash  genCacheWrite=30*(1)=30, genCacheRead=30*(2-1)=30
//   Read  genCacheWrite=15*(1)=15, genCacheRead=15*(2-1)=15
//
// turnProfiles[1] (call1, tools=[TaskCreate]):
//   toolResultCacheWrite = 80, toolResultCacheRead = 80*(1-1) = 0
//   TaskCreate fraction=1.0
//   TaskCreate genCacheWrite=5*1=5, genCacheRead=5*0=0
//
// turnProfiles[2] (call2, no tools):
//   toolResultCacheWrite = 0, toolShares = []
// ---------------------------------------------------------------------------

function skipToolModel() {
    const messages = [
        call({
            generationMs: 10,
            thinkingMs: 0,
            outputTokens: 50,
            cacheWriteTokens: 0,
            toolUses: [
                // resultTokens chosen so call0's results total 150 split 100/50
                // (Bash 2/3, Read 1/3 — same fractions the old preview-length
                // proxy produced), keeping the downstream skip expectations stable.
                toolUse("Bash", 30, "x".repeat(10), 100),
                toolUse("Read", 15, "y".repeat(5), 50),
            ],
        }),
        call({
            generationMs: 10,
            thinkingMs: 0,
            outputTokens: 20,
            cacheWriteTokens: 200,
            toolUses: [toolUse("TaskCreate", 5, "z".repeat(2), 80)],
        }),
        call({
            generationMs: 10,
            thinkingMs: 0,
            outputTokens: 10,
            cacheWriteTokens: 100,
            toolUses: [],
        }),
    ];
    return buildThinkingModel(messages, TOTALS, null);
}

describe("buildThinkingModel — toolStats", () => {
    test("lists each unique tool with correct call count and output tokens", () => {
        const m = skipToolModel();
        expect(m).not.toBeNull();
        if (!m) return;
        const byName = Object.fromEntries(m.toolStats.map((t) => [t.toolName, t]));
        expect(byName["Bash"].callCount).toBe(1);
        expect(byName["Bash"].outputTokens).toBe(30);
        expect(byName["Read"].callCount).toBe(1);
        expect(byName["Read"].outputTokens).toBe(15);
        expect(byName["TaskCreate"].callCount).toBe(1);
        expect(byName["TaskCreate"].outputTokens).toBe(5);
    });

    test("sorted by callCount descending", () => {
        // Three tools each with 1 call in this fixture; add a second Bash call
        // to verify descending order.
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 50,
                cacheWriteTokens: 0,
                toolUses: [toolUse("Bash", 30)],
            }),
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 20,
                cacheWriteTokens: 100,
                toolUses: [toolUse("Bash", 25), toolUse("Read", 10)],
            }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(m.toolStats[0].toolName).toBe("Bash"); // 2 calls
        expect(m.toolStats[0].callCount).toBe(2);
        expect(m.toolStats[0].outputTokens).toBe(55); // 30 + 25
        expect(m.toolStats[1].toolName).toBe("Read"); // 1 call
    });
});

describe("buildThinkingModel — turnProfiles", () => {
    test("toolNames matches tools called in each emission", () => {
        const m = skipToolModel();
        if (!m) return;
        expect(m.turnProfiles[0].toolNames).toEqual(["Bash", "Read"]);
        expect(m.turnProfiles[1].toolNames).toEqual(["TaskCreate"]);
        expect(m.turnProfiles[2].toolNames).toEqual([]);
    });

    test("toolResultCacheWrite/Read derived from measured result tokens", () => {
        const m = skipToolModel();
        if (!m) return;
        // call0: toolPerCall=150, later=2 → write=150, read=150*(2-1)=150
        expect(m.turnProfiles[0].toolResultCacheWrite).toBeCloseTo(150, 5);
        expect(m.turnProfiles[0].toolResultCacheRead).toBeCloseTo(150, 5);
        // call1: toolPerCall=80, later=1 → write=80, read=80*(1-1)=0
        expect(m.turnProfiles[1].toolResultCacheWrite).toBeCloseTo(80, 5);
        expect(m.turnProfiles[1].toolResultCacheRead).toBeCloseTo(0, 5);
        // call2: last call, no successor → 0
        expect(m.turnProfiles[2].toolResultCacheWrite).toBeCloseTo(0, 5);
    });

    test("toolShares resultFraction weighted by measured result tokens", () => {
        const m = skipToolModel();
        if (!m) return;
        // call0: Bash result=100, Read result=50 → 100/150, 50/150 (= 10/15, 5/15)
        const [bash, read] = m.turnProfiles[0].toolShares;
        expect(bash.toolName).toBe("Bash");
        expect(bash.resultFraction).toBeCloseTo(10 / 15, 5);
        expect(read.toolName).toBe("Read");
        expect(read.resultFraction).toBeCloseTo(5 / 15, 5);
        // call1: TaskCreate only → fraction=1.0
        expect(m.turnProfiles[1].toolShares[0].resultFraction).toBeCloseTo(1.0, 5);
    });

    test("toolShares resultFraction splits equally when no result tokens recorded", () => {
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 20,
                cacheWriteTokens: 0,
                toolUses: [toolUse("A", 10, null), toolUse("B", 10, null)],
            }),
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 10,
                cacheWriteTokens: 100,
                toolUses: [],
            }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        const [a, b] = m.turnProfiles[0].toolShares;
        expect(a.resultFraction).toBeCloseTo(0.5, 5);
        expect(b.resultFraction).toBeCloseTo(0.5, 5);
    });

    test("toolShares genCacheWrite/Read use laterInBranch multiplier", () => {
        const m = skipToolModel();
        if (!m) return;
        // call0 has later=2: genCacheWrite = genTokens*1, genCacheRead = genTokens*(2-1)
        const bash = m.turnProfiles[0].toolShares[0];
        expect(bash.genTokens).toBe(30);
        expect(bash.genCacheWrite).toBeCloseTo(30, 5); // 30 * 1
        expect(bash.genCacheRead).toBeCloseTo(30, 5);  // 30 * (2-1)
        // call1 has later=1: genCacheRead = genTokens*(1-1) = 0
        const tc = m.turnProfiles[1].toolShares[0];
        expect(tc.genTokens).toBe(5);
        expect(tc.genCacheWrite).toBeCloseTo(5, 5);  // 5 * 1
        expect(tc.genCacheRead).toBeCloseTo(0, 5);   // 5 * 0
    });
});

describe("computeSkipDelta", () => {
    test("empty skip set → zero delta", () => {
        const m = skipToolModel();
        if (!m) return;
        const d = computeSkipDelta(m.turnProfiles, new Set());
        expect(d.outputTokens).toBe(0);
        expect(d.cacheWrite).toBe(0);
        expect(d.cacheRead).toBe(0);
        expect(d.toolResultCacheWrite).toBe(0);
        expect(d.toolResultCacheRead).toBe(0);
    });

    test("solo tool skip: full turn eliminated — gen + result cascade removed", () => {
        // TaskCreate is the only tool in call1 → full elimination.
        // output: 5 (genTokens, no thinking)
        // cacheWrite: 5 (genCW) + 80 (toolResultCW) = 85
        // cacheRead:  0 (genCR) + 0  (toolResultCR) = 0
        const m = skipToolModel();
        if (!m) return;
        const d = computeSkipDelta(m.turnProfiles, new Set(["TaskCreate"]));
        expect(d.outputTokens).toBeCloseTo(5, 5);
        expect(d.cacheWrite).toBeCloseTo(85, 5);
        expect(d.cacheRead).toBeCloseTo(0, 5);
        expect(d.toolResultCacheWrite).toBeCloseTo(80, 5);
        expect(d.toolResultCacheRead).toBeCloseTo(0, 5);
    });

    test("solo tool skip eliminates full turn including thinking", () => {
        // call0 has 100 thinking tokens + one tool. Skipping the tool kills the
        // whole call, so thinking tokens also disappear.
        // thinkPerCall[0] = 300 * (10/30) = 100; later=2
        // thinkCacheWrite = 100*1 = 100, thinkCacheRead = 100*(2-1) = 100
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 10,
                outputTokens: 100,
                cacheWriteTokens: 0,
                toolUses: [toolUse("Bash", 20, "preview")],
            }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 150 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        const thinkEst = 300 * (10 / 30); // ≈ 100
        const d = computeSkipDelta(m.turnProfiles, new Set(["Bash"]));
        // output = thinking(100) + genTokens(20)
        expect(d.outputTokens).toBeCloseTo(thinkEst + 20, 3);
        // cacheWrite includes all three: thinking + gen + result cascades
        expect(d.cacheWrite).toBeGreaterThan(0);
        // thinkCacheWrite and genCacheWrite are non-zero (later=2)
        expect(d.cacheWrite).toBeCloseTo(
            m.turnProfiles[0].thinkCacheWrite +
                m.turnProfiles[0].toolShares[0].genCacheWrite +
                m.turnProfiles[0].toolResultCacheWrite,
            3,
        );
    });

    test("mixed turn partial skip: only skipped tool's gen + result share removed, thinking stays", () => {
        // call0 has Bash + Read. Skipping only Bash.
        // Bash resultFraction = 10/15, so result contribution = (10/15)*150
        const m = skipToolModel();
        if (!m) return;
        const bashFrac = 10 / 15;
        const d = computeSkipDelta(m.turnProfiles, new Set(["Bash"]));
        // output: Bash genTokens only (thinking=0 since no thinking in skipToolModel)
        expect(d.outputTokens).toBeCloseTo(30, 5);
        // cacheWrite: Bash genCW(30) + Bash resultFraction * toolResultCW(150)
        expect(d.cacheWrite).toBeCloseTo(30 + bashFrac * 150, 4);
        // cacheRead: Bash genCR(30) + Bash resultFraction * toolResultCR(150)
        expect(d.cacheRead).toBeCloseTo(30 + bashFrac * 150, 4);
        // toolResultCacheWrite only the fraction
        expect(d.toolResultCacheWrite).toBeCloseTo(bashFrac * 150, 4);
    });

    test("skipping all tools in a mixed turn triggers full elimination", () => {
        // Skip both Bash and Read in call0.
        const m = skipToolModel();
        if (!m) return;
        const d = computeSkipDelta(m.turnProfiles, new Set(["Bash", "Read"]));
        // Full elimination: gen(30+15) + result(150) cascade
        expect(d.outputTokens).toBeCloseTo(45, 5); // 30+15 gen, 0 thinking
        expect(d.cacheWrite).toBeCloseTo(
            30 + 15 + 150,   // genCW(Bash) + genCW(Read) + full toolResultCW
            4,
        );
        expect(d.toolResultCacheWrite).toBeCloseTo(150, 5); // full, not fractional
    });

    test("calls with no tools are never eliminated", () => {
        // call2 has no tools; skipping any set of tools must not touch it.
        const messages = [
            call({ generationMs: 10, thinkingMs: 10, outputTokens: 100, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        // call0 has no tools (toolUses=[]) → even with a non-empty skip set, delta = 0
        const d = computeSkipDelta(m.turnProfiles, new Set(["Bash", "Read"]));
        expect(d.outputTokens).toBe(0);
        expect(d.cacheWrite).toBe(0);
    });
});

describe("projectThinking — tool skip", () => {
    test("scale=1, no skip reproduces the baseline exactly", () => {
        const m = skipToolModel();
        if (!m) return;
        const base = projectThinking(m, 1, 1);
        expect(base.outputTokens).toBe(TOTALS.output);
        expect(base.cacheCreationTokens).toBe(TOTALS.cacheCreation);
        expect(base.cacheReadTokens).toBe(TOTALS.cacheRead);
    });

    test("skip reduces cost relative to baseline", () => {
        const m = skipToolModel();
        if (!m) return;
        const base = projectThinking(m, 1, 1);
        const skip = computeSkipDelta(m.turnProfiles, new Set(["TaskCreate"]));
        const proj = projectThinking(m, 1, 1, skip);
        expect(proj.costUsd).toBeLessThan(base.costUsd);
    });

    test("tool-output slider is a no-op when all tools are skipped", () => {
        // Skipping every tool means no remaining tool results to scale.
        // Moving toolScale should not change the projection.
        const m = skipToolModel();
        if (!m) return;
        const allTools = new Set(m.toolStats.map((t) => t.toolName));
        const skip = computeSkipDelta(m.turnProfiles, allTools);
        const atHalf = projectThinking(m, 1, 0.5, skip);
        const atOne = projectThinking(m, 1, 1, skip);
        const atDouble = projectThinking(m, 1, 2, skip);
        expect(atHalf.cacheCreationTokens).toBeCloseTo(atOne.cacheCreationTokens, 3);
        expect(atHalf.cacheReadTokens).toBeCloseTo(atOne.cacheReadTokens, 3);
        expect(atDouble.cacheCreationTokens).toBeCloseTo(atOne.cacheCreationTokens, 3);
        expect(atDouble.cacheReadTokens).toBeCloseTo(atOne.cacheReadTokens, 3);
    });

    test("tool-output slider still works on remaining tools after partial skip", () => {
        // Skip only TaskCreate; Bash + Read tool results remain and the slider
        // should still move cost up/down for those.
        const m = skipToolModel();
        if (!m) return;
        const skip = computeSkipDelta(m.turnProfiles, new Set(["TaskCreate"]));
        const atZero = projectThinking(m, 1, 0, skip);
        const atOne = projectThinking(m, 1, 1, skip);
        const atDouble = projectThinking(m, 1, 2, skip);
        expect(atZero.cacheCreationTokens).toBeLessThan(atOne.cacheCreationTokens);
        expect(atDouble.cacheCreationTokens).toBeGreaterThan(atOne.cacheCreationTokens);
    });

    test("skipping more tools saves more cost (monotone in skip set size)", () => {
        const m = skipToolModel();
        if (!m) return;
        const base = projectThinking(m, 1, 1).costUsd;
        const skipOne = projectThinking(
            m, 1, 1, computeSkipDelta(m.turnProfiles, new Set(["TaskCreate"])),
        ).costUsd;
        const skipTwo = projectThinking(
            m, 1, 1, computeSkipDelta(m.turnProfiles, new Set(["TaskCreate", "Read"])),
        ).costUsd;
        const skipAll = projectThinking(
            m, 1, 1, computeSkipDelta(m.turnProfiles, new Set(m.toolStats.map((t) => t.toolName))),
        ).costUsd;
        expect(skipOne).toBeLessThan(base);
        expect(skipTwo).toBeLessThan(skipOne);
        expect(skipAll).toBeLessThan(skipTwo);
    });

    test("skip and thinking scale compose independently", () => {
        // Skipping a tool + scaling thinking should give the same result as
        // applying each delta separately and summing — they are additive on
        // top of the baseline because they affect disjoint token populations.
        const m = skipToolModel();
        if (!m) return;
        const skip = computeSkipDelta(m.turnProfiles, new Set(["TaskCreate"]));
        const combined = projectThinking(m, 0.5, 1, skip);
        const skipOnly = projectThinking(m, 1, 1, skip);
        const thinkOnly = projectThinking(m, 0.5, 1);
        const base = projectThinking(m, 1, 1);
        // cost(combined) ≈ cost(skipOnly) + cost(thinkOnly) - cost(base) when
        // the populations are disjoint (tool skip has no thinking in this fixture).
        expect(combined.costUsd).toBeCloseTo(
            skipOnly.costUsd + thinkOnly.costUsd - base.costUsd,
            6,
        );
    });

    test("token counts never go negative under extreme skip combinations", () => {
        const m = skipToolModel();
        if (!m) return;
        const skip = computeSkipDelta(
            m.turnProfiles,
            new Set(m.toolStats.map((t) => t.toolName)),
        );
        const proj = projectThinking(m, 0, 0, skip);
        expect(proj.outputTokens).toBeGreaterThanOrEqual(0);
        expect(proj.cacheCreationTokens).toBeGreaterThanOrEqual(0);
        expect(proj.cacheReadTokens).toBeGreaterThanOrEqual(0);
    });
});

describe("projectPerMessage", () => {
    test("per-message deltas sum to the aggregate projection delta (thinking lever)", () => {
        const m = earlyThinkingModel();
        if (!m) return;
        const impacts = projectPerMessage(m, 2); // double thinking
        const sumOut = impacts.reduce((s, i) => s + i.dOutput, 0);
        const sumCw = impacts.reduce((s, i) => s + i.dCacheWrite, 0);
        const sumCr = impacts.reduce((s, i) => s + i.dCacheRead, 0);
        const agg = projectThinking(m, 2);
        const base = projectThinking(m, 1);
        expect(sumOut).toBeCloseTo(agg.outputTokens - base.outputTokens, 4);
        expect(sumCw).toBeCloseTo(agg.cacheCreationTokens - base.cacheCreationTokens, 4);
        expect(sumCr).toBeCloseTo(agg.cacheReadTokens - base.cacheReadTokens, 4);
    });

    test("attributes the thinking OUTPUT delta to the message that did the thinking", () => {
        // earlyThinkingModel: only call 0 thinks (100 tokens), index assigned in order.
        const m = earlyThinkingModel();
        if (!m) return;
        const impacts = projectPerMessage(m, 0); // remove thinking
        // OUTPUT is generated AT the thinking call, so exactly one row carries an
        // output delta — call 0 — of −100. (Its cache-write/read cascade lands on
        // the LATER calls that incur them; see the incidence test below.)
        const withOutput = impacts.filter((i) => Math.abs(i.dOutput) >= 1);
        expect(withOutput).toHaveLength(1);
        expect(withOutput[0].dOutput).toBeCloseTo(-100, 5);
        // Total output delta across all rows equals the single source's.
        const sumOut = impacts.reduce((s, i) => s + i.dOutput, 0);
        expect(sumOut).toBeCloseTo(-100, 5);
    });

    test("flags an eliminated turn when all its tools are skipped", () => {
        const m = skipToolModel();
        if (!m) return;
        // TaskCreate is the only tool in call1 → skipping it eliminates that turn.
        const impacts = projectPerMessage(m, 1, 1, new Set(["TaskCreate"]));
        const elim = impacts.filter((i) => i.eliminated);
        expect(elim).toHaveLength(1);
        expect(elim[0].toolNames).toEqual(["TaskCreate"]);
    });

    test("identity levers + no skip → no message moves", () => {
        const m = toolModel();
        if (!m) return;
        const impacts = projectPerMessage(m, 1, 1, new Set());
        expect(impacts.every((i) => i.magnitude < 1e-9)).toBe(true);
    });
});

// Mirrors the real claude_subagent_test shape: a standalone Write call whose
// own prompt re-reads ~20k cached tokens. Skipping Write must make that whole
// row disappear — not just zero its output. The cache-READ + input vanish (one
// fewer reader); the prompt cache-WRITE relocates to the next surviving call.
function writeRowModel() {
    const messages = [
        // call0: opening thinking that establishes the cached context.
        call({ generationMs: 10, thinkingMs: 10, outputTokens: 65, cacheWriteTokens: 20000 }),
        // call1: the standalone Write call — re-reads the 20k prefix.
        {
            ...call({ generationMs: 10, thinkingMs: 0, outputTokens: 126, cacheWriteTokens: 200, toolUses: [toolUse("Write", 126, "wrote results.txt", 204)] }),
            inputTokens: 1,
            cacheReadTokens: 20000,
        },
        // call2: final text reply.
        {
            ...call({ generationMs: 10, thinkingMs: 0, outputTokens: 29, cacheWriteTokens: 130 }),
            inputTokens: 1,
            cacheReadTokens: 20300,
        },
    ];
    const totals: TokenTotals = { input: 3, output: 220, cacheCreation: 20330, cacheRead: 40300, total: 60853 };
    return { m: buildThinkingModel(messages, totals, null), messages };
}

describe("projectPerMessage — turn elimination removes the whole row", () => {
    test("skipping the sole tool zeroes the row's full footprint (input + cache-read + cache-write + output)", () => {
        const { m, messages } = writeRowModel();
        if (!m) return;
        const writeIdx = messages[1].index;
        const impacts = projectPerMessage(m, 1, 1, new Set(["Write"]));
        const row = impacts.find((i) => i.messageIndex === writeIdx)!;
        expect(row.eliminated).toBe(true);
        // Each delta exactly cancels the row's recorded bucket → the row vanishes.
        expect(row.dOutput).toBeCloseTo(-126, 5); // its generation
        expect(row.dInput).toBeCloseTo(-1, 5); // own uncached prompt
        expect(row.dCacheRead).toBeCloseTo(-20000, 5); // own prefix re-read — the dominant saving
        expect(row.dCacheWrite).toBeCloseTo(-200, 5); // own prompt cache-write (relocated away)
    });

    test("the eliminated call's prompt cache-write relocates to the next surviving call", () => {
        const { m, messages } = writeRowModel();
        if (!m) return;
        const nextIdx = messages[2].index;
        const impacts = projectPerMessage(m, 1, 1, new Set(["Write"]));
        const next = impacts.find((i) => i.messageIndex === nextIdx)!;
        // call2 receives Write's relocated 200-token prompt cache-write on top of
        // losing Write's sourced cascade (genCW 126 + resultCW 204 = 330):
        //   −330 (sourced removed) + 200 (relocated) = −130.
        expect(next.dCacheWrite).toBeCloseTo(-130, 5);
    });

    test("per-row deltas still sum to the aggregate projection delta (incl. input + dominant cache-read)", () => {
        const { m } = writeRowModel();
        if (!m) return;
        const skip = computeSkipDelta(m.turnProfiles, new Set(["Write"]));
        const agg = projectThinking(m, 1, 1, skip);
        const base = projectThinking(m, 1, 1);
        const impacts = projectPerMessage(m, 1, 1, new Set(["Write"]));
        const sum = (sel: (i: (typeof impacts)[number]) => number) =>
            impacts.reduce((s, i) => s + sel(i), 0);
        expect(sum((i) => i.dInput)).toBeCloseTo(agg.inputTokens - base.inputTokens, 4);
        expect(sum((i) => i.dOutput)).toBeCloseTo(agg.outputTokens - base.outputTokens, 4);
        expect(sum((i) => i.dCacheWrite)).toBeCloseTo(agg.cacheCreationTokens - base.cacheCreationTokens, 4);
        expect(sum((i) => i.dCacheRead)).toBeCloseTo(agg.cacheReadTokens - base.cacheReadTokens, 4);
        // The dominant saving is the ~20k prompt cache-read, not the 126 output.
        expect(base.cacheReadTokens - agg.cacheReadTokens).toBeCloseTo(20000, 4);
    });

    test("computeSkipDelta books the eliminated call's own prompt read + input", () => {
        const { m } = writeRowModel();
        if (!m) return;
        const d = computeSkipDelta(m.turnProfiles, new Set(["Write"]));
        expect(d.cacheRead).toBeCloseTo(20000, 5); // own prefix re-read (0 sourced read here)
        expect(d.input).toBeCloseTo(1, 5);
        // cacheWrite is sourced-only (330); the 200 own-write relocated (net 0).
        expect(d.cacheWrite).toBeCloseTo(330, 5);
    });
});

describe("projectThinking — robust across ALL lever combinations", () => {
    // A reconciliation entry is part of the real stream now; buildThinkingModel
    // must ignore it and the projection must stay finite + non-negative for
    // every (thinkScale, toolScale, skip-subset) combination, including on a run
    // with caching disabled (cacheCreation == cacheRead == 0).
    function buildWith(totals: TokenTotals) {
        const messages = [
            call({
                generationMs: 10,
                thinkingMs: 5,
                outputTokens: 100,
                cacheWriteTokens: 0,
                toolUses: [toolUse("Bash", 30, "x".repeat(10), 400)],
            }),
            call({
                generationMs: 10,
                thinkingMs: 0,
                outputTokens: 50,
                cacheWriteTokens: totals.cacheCreation > 0 ? 300 : 0,
                toolUses: [toolUse("Read", 15, "y".repeat(5), 200)],
            }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 0 }),
        ];
        return buildThinkingModel(messages, totals, 0.5);
    }

    const SCALES = [0, 0.5, 1, 1.5, 2];

    function assertSane(p: ReturnType<typeof projectThinking>) {
        for (const v of [
            p.outputTokens,
            p.cacheCreationTokens,
            p.cacheReadTokens,
            p.inputTokens,
            p.totalTokens,
            p.costUsd,
        ]) {
            expect(Number.isFinite(v)).toBe(true);
            expect(v).toBeGreaterThanOrEqual(0);
        }
    }

    test("cached run: every (think, tool, skip) combo is finite and non-negative", () => {
        const m = buildWith({ input: 10, output: 200, cacheCreation: 300, cacheRead: 1000, total: 1510 });
        if (!m) throw new Error("model should build");
        const toolSubsets = [new Set<string>(), new Set(["Bash"]), new Set(["Bash", "Read"])];
        for (const s of SCALES) {
            for (const t of SCALES) {
                for (const skipped of toolSubsets) {
                    assertSane(projectThinking(m, s, t, computeSkipDelta(m.turnProfiles, skipped)));
                }
            }
        }
    });

    test("cache-disabled run: tool lever WORKS (content-based) and every combo stays sane", () => {
        // No prompt caching: cacheCreation == cacheRead == 0. Tool-result size is
        // still measured from content, so the lever is active (routes to input);
        // every (think, tool, skip) combo must stay finite + non-negative.
        const m = buildWith({ input: 5000, output: 200, cacheCreation: 0, cacheRead: 0, total: 5200 });
        if (!m) throw new Error("model should build even with caching disabled");
        expect(m.hasCacheWrite).toBe(false);
        expect(m.hasCacheRead).toBe(false);
        expect(m.toolResultTokens).toBeGreaterThan(0); // measured from content, not cache
        // Moving the tool lever changes cost even with caching off.
        const lo = projectThinking(m, 1, 0, computeSkipDelta(m.turnProfiles, new Set()));
        const hi = projectThinking(m, 1, 2, computeSkipDelta(m.turnProfiles, new Set()));
        expect(hi.costUsd).toBeGreaterThan(lo.costUsd);
        const toolSubsets = [new Set<string>(), new Set(["Bash"]), new Set(["Bash", "Read"])];
        for (const s of SCALES) {
            for (const t of SCALES) {
                for (const skipped of toolSubsets) {
                    assertSane(projectThinking(m, s, t, computeSkipDelta(m.turnProfiles, skipped)));
                }
            }
        }
    });

    test("a reconciliation message in the stream is excluded from the model", () => {
        const base = [
            call({ generationMs: 10, thinkingMs: 5, outputTokens: 100, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
        ];
        const withRecon = [
            ...base,
            // role=reconciliation rows carry only token buckets + a note.
            { ...base[0], role: "reconciliation" as const, generationMs: null, thinkingMs: null },
        ];
        const a = buildThinkingModel(base, TOTALS, 0.5);
        const b = buildThinkingModel(withRecon, TOTALS, 0.5);
        expect(a).not.toBeNull();
        expect(b).not.toBeNull();
        if (!a || !b) return;
        // Same number of real calls — the reconciliation row didn't inflate it.
        expect(b.calls).toBe(a.calls);
        expect(b.coeffOutput).toBeCloseTo(a.coeffOutput, 5);
    });
});
