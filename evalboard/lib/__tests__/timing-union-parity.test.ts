import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { busyMs, toolExecutionMs } from "../timing";
import type { MessageEvent, MessageToolUse } from "../runs";

// Parity guard: `busyMs` here and `coder_eval.timing.busy_ms` in the
// Python harness answer the same question about the same task.json — how much
// wall clock the tools occupied — one to subtract it from a generation window,
// the other to subtract it from the task's duration. A divergence makes the
// evalboard's Unaccounted residual disagree with the harness's own generation
// figures, which is exactly the class of bug this file exists to catch.
//
// Neither side owns the numbers: tests/_fixtures/timing_union_cases.json does,
// and tests/test_timing_union_parity.py replays the identical array.

const here = dirname(fileURLToPath(import.meta.url));
const fixture = resolve(here, "../../../tests/_fixtures/timing_union_cases.json");

interface UnionCase {
    name: string;
    window: [number, number];
    spans: [number, number][];
    expected_ms: number;
}

// `union_cases` carries no window: the extent is the spans' own bounds. It is
// the half that pins `toolExecutionMs`, which derives that extent with its own
// min/max instead of being handed one — the Python twin is
// `coder_eval.timing.union_ms`.
interface ExtentCase {
    name: string;
    spans: [number, number][];
    expected_ms: number;
}

// `unbounded_cases` pins the POLICY rather than the arithmetic: a call the
// harness TIMED but did not BOUND contributes nothing to the union. Python
// replays these through the production selector
// `coder_eval.timing.main_thread_tool_spans`, whose `is not None` filter IS
// that policy; `toolExecutionMs` makes the same decision inline.
interface UnboundedCase {
    name: string;
    spans: [number, number][];
    unbounded_ms: number[];
    expected_ms: number;
}

const corpus: {
    cases: UnionCase[];
    union_cases: ExtentCase[];
    unbounded_cases: UnboundedCase[];
} = JSON.parse(readFileSync(fixture, "utf8"));

describe("busyMs matches the shared union corpus", () => {
    test("the corpus is non-empty (a silently emptied file must not pass)", () => {
        expect(corpus.cases.length).toBeGreaterThan(10);
    });

    for (const c of corpus.cases) {
        test(c.name, () => {
            expect(busyMs(c.spans, c.window[0], c.window[1])).toBeCloseTo(
                c.expected_ms,
                6,
            );
        });
    }
});

function toolUse(over: Partial<MessageToolUse>): MessageToolUse {
    return {
        toolName: "Bash",
        toolUseId: "t1",
        summary: "",
        argText: null,
        description: null,
        genMs: null,
        durationMs: null,
        isError: false,
        resultPreview: null,
        outputTokens: null,
        resultTokens: null,
        execStartMs: null,
        execEndMs: null,
        ...over,
    };
}

function message(toolUses: MessageToolUse[]): MessageEvent {
    return {
        index: 1,
        role: "assistant",
        startedAt: null,
        completedAt: null,
        generationMs: null,
        thinkingMs: null,
        textMs: null,
        toolGenMs: null,
        mixedGenMs: null,
        blockTypes: [],
        thinkingText: null,
        text: null,
        toolUses,
        inputTokens: null,
        outputTokens: null,
        cacheWriteTokens: null,
        cacheReadTokens: null,
        reasoningTokens: null,
        thinkingOutputTokens: null,
        textOutputTokens: null,
        model: null,
        parentToolUseId: null,
        costUsd: null,
        note: null,
    };
}

describe("toolExecutionMs", () => {
    test("concurrent calls occupy the wall clock once", () => {
        // The defect this replaces: summing durationMs reported 10s for two
        // 5s calls that ran side by side, which drove Unaccounted negative.
        const ms = toolExecutionMs([
            message([
                toolUse({ execStartMs: 1000, execEndMs: 6000, durationMs: 5000 }),
                toolUse({ execStartMs: 2000, execEndMs: 7000, durationMs: 5000 }),
            ]),
        ]);
        expect(ms).toBe(6000);
    });

    test("sequential calls still add up", () => {
        const ms = toolExecutionMs([
            message([
                toolUse({ execStartMs: 1000, execEndMs: 2000, durationMs: 1000 }),
                toolUse({ execStartMs: 3000, execEndMs: 3500, durationMs: 500 }),
            ]),
        ]);
        expect(ms).toBe(1500);
    });

    test("the union spans messages, not just one message's own calls", () => {
        const ms = toolExecutionMs([
            message([toolUse({ execStartMs: 1000, execEndMs: 6000 })]),
            message([toolUse({ execStartMs: 2000, execEndMs: 7000 })]),
        ]);
        expect(ms).toBe(6000);
    });

    test("a timed call with no bounds contributes nothing", () => {
        // It used to add its own durationMs, which was the one policy on which
        // the two languages disagreed — every Python surface has always dropped
        // it (main_thread_tool_spans filters on `is not None`). A duration with
        // no bounds cannot be placed on the timeline, so it cannot be unioned;
        // folding it in double-books whatever it overlapped. Its time reads as
        // Unaccounted instead.
        const ms = toolExecutionMs([
            message([
                toolUse({ execStartMs: 1000, execEndMs: 2000 }),
                toolUse({ durationMs: 250 }),
            ]),
        ]);
        expect(ms).toBe(1000);
    });

    test("a call the harness never timed contributes nothing", () => {
        expect(toolExecutionMs([message([toolUse({})])])).toBe(0);
    });
});

describe("toolExecutionMs matches the shared extent corpus", () => {
    test("the extent corpus is non-empty (a silently emptied file must not pass)", () => {
        expect(corpus.union_cases.length).toBeGreaterThan(5);
    });

    // Each span becomes one bounded tool call on one message, so
    // `toolExecutionMs` has to derive the extent itself — the one part of the
    // union rule the windowed `cases` above cannot reach. Python replays the
    // same array through `coder_eval.timing.union_ms`.
    for (const c of corpus.union_cases) {
        test(c.name, () => {
            const ms = toolExecutionMs([
                message(
                    c.spans.map(([s, e], i) =>
                        toolUse({
                            toolUseId: `t${i}`,
                            execStartMs: s,
                            execEndMs: e,
                        }),
                    ),
                ),
            ]);
            expect(ms).toBeCloseTo(c.expected_ms, 6);
        });
    }
});

describe("toolExecutionMs matches the shared unbounded-policy corpus", () => {
    test("the policy corpus is non-empty (a silently emptied file must not pass)", () => {
        expect(corpus.unbounded_cases.length).toBeGreaterThan(2);
    });

    // Bounded spans become execStartMs/execEndMs calls; unbounded ones become
    // durationMs-only calls. `expected_ms` is the union of the bounded spans
    // alone, so a side that folds the unbounded durations back in fails here.
    for (const c of corpus.unbounded_cases) {
        test(c.name, () => {
            const ms = toolExecutionMs([
                message([
                    ...c.spans.map(([s, e], i) =>
                        toolUse({
                            toolUseId: `b${i}`,
                            execStartMs: s,
                            execEndMs: e,
                        }),
                    ),
                    ...c.unbounded_ms.map((durationMs, i) =>
                        toolUse({ toolUseId: `u${i}`, durationMs }),
                    ),
                ]),
            ]);
            expect(ms).toBeCloseTo(c.expected_ms, 6);
        });
    }
});
