import { describe, expect, test } from "vitest";
import type { MessageEvent, MessageToolUse, TokenTotals } from "../runs";
import {
    buildThinkingModel,
    computeSkipDelta,
    projectThinking,
    thinkingAmplification,
    toolAmplification,
} from "../thinkingSim";
import { resolvePricing } from "../pricing";

// Minimal MessageToolUse for skip-tool tests.
function toolUse(
    toolName: string,
    outputTokens: number,
    resultPreview: string | null = null,
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
    };
}

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
    toolUses?: MessageToolUse[];
    // null = main thread (default), string = sub-agent branch, undefined =
    // run didn't record branch info (legacy → simulator disabled).
    parentToolUseId?: string | null;
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

    test("folds the sub-agent footprint into the tool-output lever", () => {
        // Same 3-call run as toolModel(): message-derived write 300 / read 200.
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 150 }),
        ];
        // A sub-agent contributes cache-creation 1000 → tool write, cache-read
        // 8975 → tool read (one-time, no extra main-thread cascade).
        const subAgents = [
            { total: 10000, input: 5, output: 20, cacheCreation: 1000, cacheRead: 8975 },
        ];
        const m = buildThinkingModel(messages, TOTALS, 0.5, subAgents);
        if (!m) return;
        expect(m.coeffToolWrite).toBeCloseTo(1300, 5); // 300 + 1000
        expect(m.coeffToolRead).toBeCloseTo(9175, 5); // 200 + 8975
        // Empty sub-agent list leaves the lever exactly as the message cascade.
        const bare = buildThinkingModel(messages, TOTALS, 0.5, []);
        if (!bare) return;
        expect(bare.coeffToolWrite).toBeCloseTo(300, 5);
        expect(bare.coeffToolRead).toBeCloseTo(200, 5);
    });

    test("parallel intermediates: group boundary subtracts combined group output, not just its own", () => {
        // 4 messages, no thinking:
        //   call0: cw=0,   out=100 — pre-boundary (no cw, first message)
        //   call1: cw=300, out=50  — boundary B_0 (group = [call0])
        //   call2: cw=0,   out=30  — intermediate parallel message
        //   call3: cw=150, out=20  — boundary B_1 (group = [call1, call2])
        //
        // toolPerCall[j]:
        //   j=0: next=call1 cw=300>0 → boundary
        //        loop: k=0, cw=0, add 100, k=-1 → stop
        //        groupRealOutput=100 → toolPerCall[0] = max(0, 300-100) = 200
        //   j=1: next=call2 cw=0 → NOT a boundary → 0
        //   j=2: next=call3 cw=150>0 → boundary
        //        loop: k=2, cw=0, add 30, continue
        //              k=1, cw=300>0, add 50, BREAK
        //        groupRealOutput=80 → toolPerCall[2] = max(0, 150-80) = 70
        //
        // Old formula would give toolPerCall[2] = max(0,150-30) = 120 (wrong, ignores call1's 50)
        //
        // coeffToolWrite = 200 + 70 = 270
        // laterInBranch[0]=3, later-1=2 → coeffToolRead += 200*2 = 400
        // laterInBranch[2]=1, later-1=0 → coeffToolRead += 70*0 = 0
        // coeffToolRead = 400
        const messages = [
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 100, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 50, cacheWriteTokens: 300 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 30, cacheWriteTokens: 0 }),
            call({ generationMs: 10, thinkingMs: 0, outputTokens: 20, cacheWriteTokens: 150 }),
        ];
        const m = buildThinkingModel(messages, TOTALS, null);
        if (!m) return;
        expect(m.coeffToolWrite).toBeCloseTo(270, 5);
        expect(m.coeffToolRead).toBeCloseTo(400, 5);
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
                toolUse("Bash", 30, "x".repeat(10)),
                toolUse("Read", 15, "y".repeat(5)),
            ],
        }),
        call({
            generationMs: 10,
            thinkingMs: 0,
            outputTokens: 20,
            cacheWriteTokens: 200,
            toolUses: [toolUse("TaskCreate", 5, "z".repeat(2))],
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

    test("toolResultCacheWrite/Read computed from cache-growth estimation", () => {
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

    test("toolShares resultFraction weighted by result preview length", () => {
        const m = skipToolModel();
        if (!m) return;
        // call0: Bash preview=10chars, Read preview=5chars → 10/15, 5/15
        const [bash, read] = m.turnProfiles[0].toolShares;
        expect(bash.toolName).toBe("Bash");
        expect(bash.resultFraction).toBeCloseTo(10 / 15, 5);
        expect(read.toolName).toBe("Read");
        expect(read.resultFraction).toBeCloseTo(5 / 15, 5);
        // call1: TaskCreate only → fraction=1.0
        expect(m.turnProfiles[1].toolShares[0].resultFraction).toBeCloseTo(1.0, 5);
    });

    test("toolShares resultFraction splits equally when all previews are null", () => {
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
