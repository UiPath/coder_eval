import { describe, expect, test } from "vitest";
import { approxTokens, kindWeights, parseMessages, type TurnEntry } from "../runs";

// Helper: a single assistant MessageEntry with one content block. coder_eval
// emits one message-entry per content-block kind, which is the shape these
// tests model.
function msg(
    kind: "thinking" | "text" | "tool_use",
    opts: {
        startedAt: string;
        completedAt: string;
        genMs: number | null;
        text?: string;
        thinking?: string;
        toolUseId?: string;
    },
) {
    const block: Record<string, unknown> = { block_type: kind };
    if (kind === "thinking") block.thinking = opts.thinking ?? "T";
    if (kind === "text") block.text = opts.text ?? "hello";
    if (kind === "tool_use") block.tool_use_id = opts.toolUseId ?? null;
    return {
        role: "assistant",
        started_at: opts.startedAt,
        completed_at: opts.completedAt,
        generation_duration_ms: opts.genMs,
        content_blocks: [block],
    };
}

describe("parseMessages — per-block-kind generation attribution", () => {
    test("thinking gen attributes only to thinkingMs, not text/tool", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    msg("thinking", {
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:05.000Z",
                        genMs: 5000,
                    }),
                    // separate emission > 100ms gap so it doesn't collapse
                    msg("text", {
                        startedAt: "2026-01-01T00:00:06.000Z",
                        completedAt: "2026-01-01T00:00:07.000Z",
                        genMs: 1000,
                    }),
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(2);
        const [t, x] = events;
        expect(t.thinkingMs).toBe(5000);
        expect(t.textMs).toBeNull();
        expect(t.toolGenMs).toBeNull();
        expect(x.thinkingMs).toBeNull();
        expect(x.textMs).toBe(1000);
    });

    test("collapsed mixed emission attributes each raw's gen to its own kind", () => {
        // Same emission (gap < 100ms) → one MessageEvent with two block kinds.
        const turns: TurnEntry[] = [
            {
                messages: [
                    msg("thinking", {
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:08.000Z",
                        genMs: 8000,
                    }),
                    msg("tool_use", {
                        startedAt: "2026-01-01T00:00:08.010Z", // 10ms gap
                        completedAt: "2026-01-01T00:00:08.500Z",
                        genMs: 500,
                        toolUseId: "tu_1",
                    }),
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "tu_1",
                        parameters: { command: "ls" },
                        duration_ms: 42,
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(1);
        const e = events[0];
        expect(e.generationMs).toBe(8500);
        expect(e.thinkingMs).toBe(8000);
        expect(e.toolGenMs).toBe(500);
        expect(e.textMs).toBeNull();
        // The thinking-share inflation bug would have put 8500 here.
    });

    test("parallel tool_uses split gen weighted by token proxy", () => {
        // Two parallel tool_uses in one raw. Big-args tool gets most of the gen.
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "small" },
                            { block_type: "tool_use", tool_use_id: "big" },
                        ],
                    },
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "small",
                        parameters: { command: "ls" }, // tiny
                        duration_ms: 10,
                    },
                    {
                        tool_name: "Write",
                        tool_id: "big",
                        parameters: { file_path: "x", content: "x".repeat(400) }, // ~100x larger
                        duration_ms: 20,
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        const [e] = events;
        expect(e.toolUses).toHaveLength(2);
        const [small, big] = e.toolUses;
        expect(small.genMs).not.toBeNull();
        expect(big.genMs).not.toBeNull();
        // Both contributions must sum (approximately) to the raw gen.
        expect((small.genMs ?? 0) + (big.genMs ?? 0)).toBeCloseTo(1000, 5);
        // Bigger arg gets the larger share.
        expect(big.genMs!).toBeGreaterThan(small.genMs!);
    });

    test("parallel tool_uses with no params fall back to even split", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "a" },
                            { block_type: "tool_use", tool_use_id: "b" },
                        ],
                    },
                ],
                // No matching commands → params default to {} → token proxy = 0
                commands: [],
            },
        ];
        const events = parseMessages(turns);
        const [a, b] = events[0].toolUses;
        expect(a.genMs).toBeCloseTo(500, 5);
        expect(b.genMs).toBeCloseTo(500, 5);
    });

    test("missing generation_duration_ms leaves all gen fields null", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    msg("thinking", {
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:01.000Z",
                        genMs: null,
                    }),
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.generationMs).toBeNull();
        expect(e.thinkingMs).toBeNull();
        expect(e.textMs).toBeNull();
        expect(e.toolGenMs).toBeNull();
    });
});

describe("parseMessages — message_id collapsing", () => {
    test("collapses splits sharing a message_id even when wall-clock gap is large", () => {
        // The CLI emits per-block-kind events back-to-back, but if a slow tool
        // result lands between them the gap heuristic would (incorrectly) split
        // them. With a shared message_id we collapse anyway.
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:05.000Z",
                        generation_duration_ms: 5000,
                        message_id: "msg_abc",
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                    {
                        role: "assistant",
                        // Far apart in wall-clock — gap heuristic would split.
                        started_at: "2026-01-01T00:00:30.000Z",
                        completed_at: "2026-01-01T00:00:30.500Z",
                        generation_duration_ms: 500,
                        message_id: "msg_abc",
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "tu_1" },
                        ],
                    },
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "tu_1",
                        parameters: { command: "ls" },
                        duration_ms: 10,
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(1);
        const e = events[0];
        expect(e.blockTypes).toEqual(["thinking", "tool_use"]);
        expect(e.generationMs).toBe(5500);
    });

    test("splits when message_ids differ even with tight gap", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_a",
                        content_blocks: [{ block_type: "thinking", thinking: "x" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:01.010Z", // 10ms gap
                        completed_at: "2026-01-01T00:00:02.000Z",
                        generation_duration_ms: 990,
                        message_id: "msg_b",
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(2);
    });

    test("falls back to gap heuristic when message_id absent (legacy runs)", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "thinking", thinking: "x" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:01.050Z", // 50ms gap — under threshold
                        completed_at: "2026-01-01T00:00:01.200Z",
                        generation_duration_ms: 150,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(1);
        expect(events[0].blockTypes).toEqual(["thinking", "text"]);
    });
});

describe("parseMessages — per-message token aggregation", () => {
    test("threads input/output/cache/reasoning tokens onto the event", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_1",
                        input_tokens: 12,
                        output_tokens: 200,
                        cache_creation_tokens: 5_000,
                        cache_read_tokens: 80_000,
                        reasoning_tokens: 40,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.inputTokens).toBe(12);
        expect(e.outputTokens).toBe(200);
        expect(e.cacheWriteTokens).toBe(5_000);
        expect(e.cacheReadTokens).toBe(80_000);
        expect(e.reasoningTokens).toBe(40);
    });

    test("sums per-message token fields across same-emission splits", () => {
        // Two raws sharing one message_id collapse into one MessageEvent — the
        // CLI splits content blocks across rows but the tokens are reported on
        // each row, so the event should carry the sum.
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_x",
                        input_tokens: 2,
                        output_tokens: 100,
                        cache_creation_tokens: 1_000,
                        cache_read_tokens: 10_000,
                        reasoning_tokens: 50,
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:01.020Z",
                        completed_at: "2026-01-01T00:00:01.500Z",
                        generation_duration_ms: 480,
                        message_id: "msg_x",
                        input_tokens: 1,
                        output_tokens: 30,
                        cache_creation_tokens: 200,
                        cache_read_tokens: 0,
                        reasoning_tokens: 0,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.inputTokens).toBe(3);
        expect(e.outputTokens).toBe(130);
        expect(e.cacheWriteTokens).toBe(1_200);
        expect(e.cacheReadTokens).toBe(10_000);
        expect(e.reasoningTokens).toBe(50);
    });

    test("legacy messages (no per-message tokens) leave token fields null", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    msg("text", {
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:01.000Z",
                        genMs: 1000,
                    }),
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.inputTokens).toBeNull();
        expect(e.outputTokens).toBeNull();
        expect(e.cacheWriteTokens).toBeNull();
        expect(e.cacheReadTokens).toBeNull();
        expect(e.reasoningTokens).toBeNull();
        expect(e.textOutputTokens).toBeNull();
    });
});

describe("parseMessages — per-message cost", () => {
    test("prices the threaded tokens against the message's model", () => {
        // claude-sonnet-4-6: input 3, output 15, cacheWrite 3.75, cacheRead 0.3 /MTok.
        // (12·3 + 200·15 + 5000·3.75 + 80000·0.3)/1e6 = 0.045786
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_1",
                        input_tokens: 12,
                        output_tokens: 200,
                        cache_creation_tokens: 5_000,
                        cache_read_tokens: 80_000,
                        model: "claude-sonnet-4-6",
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.costUsd).toBeCloseTo(0.045786, 9);
    });

    test("prices the summed tokens across same-emission splits", () => {
        // Two splits collapse to one event; cost is over the summed tokens:
        // input 3, output 130, cacheWrite 1200, cacheRead 10000.
        // (3·3 + 130·15 + 1200·3.75 + 10000·0.3)/1e6 = 0.009459
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_x",
                        input_tokens: 2,
                        output_tokens: 100,
                        cache_creation_tokens: 1_000,
                        cache_read_tokens: 10_000,
                        model: "claude-sonnet-4-6",
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:01.020Z",
                        completed_at: "2026-01-01T00:00:01.500Z",
                        generation_duration_ms: 480,
                        message_id: "msg_x",
                        input_tokens: 1,
                        output_tokens: 30,
                        cache_creation_tokens: 200,
                        cache_read_tokens: 0,
                        model: "claude-sonnet-4-6",
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.costUsd).toBeCloseTo(0.009459, 9);
    });

    test("costUsd is null when the model is absent (legacy runs)", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_1",
                        input_tokens: 12,
                        output_tokens: 200,
                        cache_creation_tokens: 5_000,
                        cache_read_tokens: 80_000,
                        // no model recorded
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.costUsd).toBeNull();
    });

    test("costUsd is null when no per-message tokens were recorded", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    msg("text", {
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:01.000Z",
                        genMs: 1000,
                    }),
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.costUsd).toBeNull();
    });
});

describe("parseMessages — per-block output-token attribution", () => {
    test("text-only message attributes all output to text (no thinking block)", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_t",
                        output_tokens: 120,
                        content_blocks: [{ block_type: "text", text: "ok" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        // No thinking block → nothing carved out; all output is the text share.
        expect(e.thinkingOutputTokens).toBeNull();
        expect(e.textOutputTokens).toBe(120);
    });

    test("tool-only message attributes output across tools by gen weight", () => {
        // Same emission with two parallel tool_uses; outputTokens should
        // split by argument-size weight (the genMs split used elsewhere).
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_tools",
                        output_tokens: 220,
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "small" },
                            { block_type: "tool_use", tool_use_id: "big" },
                        ],
                    },
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "small",
                        parameters: { command: "ls" },
                    },
                    {
                        tool_name: "Write",
                        tool_id: "big",
                        parameters: { file_path: "x", content: "x".repeat(400) },
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.textOutputTokens).toBeNull();
        const [small, big] = e.toolUses;
        expect(small.outputTokens).not.toBeNull();
        expect(big.outputTokens).not.toBeNull();
        // No thinking block → full 220 goes to the tool budget.
        expect(
            (small.outputTokens ?? 0) + (big.outputTokens ?? 0),
        ).toBeCloseTo(220, 0);
        expect(big.outputTokens!).toBeGreaterThan(small.outputTokens!);
    });

    test("mixed message: thinking taken from its emission, rest split by gen-time", () => {
        // Post-fix shape: the agent distributes the call's output across its
        // block-emissions, so each carries its own output_tokens (40 thinking
        // + 50 tool + 150 text = 240). Each block's share is read straight from
        // its emission — no gen-time re-splitting.
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:08.000Z",
                        generation_duration_ms: 8000,
                        message_id: "msg_mix",
                        output_tokens: 40,
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:08.010Z",
                        completed_at: "2026-01-01T00:00:08.500Z",
                        generation_duration_ms: 500,
                        message_id: "msg_mix",
                        output_tokens: 50,
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "tu_1" },
                        ],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:08.520Z",
                        completed_at: "2026-01-01T00:00:10.020Z",
                        generation_duration_ms: 1500,
                        message_id: "msg_mix",
                        output_tokens: 150,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                    },
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "tu_1",
                        parameters: { command: "ls" },
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        // Sanity: collapsed into one event with all three block kinds.
        expect(e.blockTypes).toEqual(["thinking", "tool_use", "text"]);
        expect(e.outputTokens).toBe(240);
        // Thinking is the thinking emission's real output (40), not gen-time.
        expect(e.thinkingOutputTokens).toBe(40);
        const nonThinking = 240 - 40;
        // text share = 1500/(1500+500) = 0.75 of the remaining budget.
        expect(e.textOutputTokens).toBe(Math.round(nonThinking * 0.75));
        const toolSum = e.toolUses.reduce(
            (s, t) => s + (t.outputTokens ?? 0),
            0,
        );
        expect(toolSum).toBe(nonThinking - (e.textOutputTokens ?? 0));
    });

    test("missing output_tokens leaves per-block approximations null", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        message_id: "msg_n",
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "tu_1" },
                        ],
                    },
                ],
                commands: [
                    {
                        tool_name: "Bash",
                        tool_id: "tu_1",
                        parameters: { command: "ls" },
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.textOutputTokens).toBeNull();
        expect(e.toolUses[0].outputTokens).toBeNull();
    });
});

describe("approxTokens", () => {
    test("scales with serialized argument size", () => {
        const small = approxTokens({ cmd: "ls" });
        const big = approxTokens({ cmd: "ls", body: "x".repeat(400) });
        expect(big).toBeGreaterThan(small);
    });

    test("returns 0 for null/undefined/empty", () => {
        expect(approxTokens(null)).toBe(0);
        expect(approxTokens(undefined)).toBe(0);
        // {} serializes to "{}" → 1 token (ceil 2/4)
        expect(approxTokens({})).toBe(1);
    });
});

describe("parseMessages — per-message token summing", () => {
    // The CLI repeats one usage dict across every event sharing a message_id,
    // and only the first event carries real values; the rest are zeroed. A
    // straight sum across the collapsed group must therefore recover the call's
    // true usage (no double-counting).
    test("sums token fields across one message_id group", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:05.000Z",
                        generation_duration_ms: 5000,
                        message_id: "msg_1",
                        input_tokens: 3,
                        output_tokens: 120,
                        cache_creation_tokens: 50,
                        cache_read_tokens: 9000,
                        reasoning_tokens: 0,
                        model: "claude-sonnet-4-6",
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:05.010Z",
                        completed_at: "2026-01-01T00:00:05.020Z",
                        generation_duration_ms: 10,
                        message_id: "msg_1",
                        input_tokens: 0,
                        output_tokens: 0,
                        cache_creation_tokens: 0,
                        cache_read_tokens: 0,
                        reasoning_tokens: 0,
                        model: "claude-sonnet-4-6",
                        content_blocks: [{ block_type: "tool_use", tool_use_id: null }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.outputTokens).toBe(120);
        expect(e.cacheReadTokens).toBe(9000);
        expect(e.cacheWriteTokens).toBe(50);
        expect(e.inputTokens).toBe(3);
        expect(e.model).toBe("claude-sonnet-4-6");
    });

    test("legacy messages with no token fields surface null", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:05.000Z",
                        generation_duration_ms: 5000,
                        content_blocks: [{ block_type: "thinking", thinking: "T" }],
                    },
                ],
            },
        ];
        const [e] = parseMessages(turns);
        expect(e.outputTokens).toBeNull();
        expect(e.cacheReadTokens).toBeNull();
        expect(e.model).toBeNull();
    });
});

describe("parseMessages — reconciliation entry", () => {
    test("surfaces a role=reconciliation entry as its own event after the turn's messages", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "assistant",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                        input_tokens: 100,
                        output_tokens: 40,
                        cache_read_tokens: 2000,
                    },
                    {
                        role: "reconciliation",
                        input_tokens: 512,
                        output_tokens: 0,
                        cache_creation_tokens: 1000,
                        cache_read_tokens: 0,
                        note: "billed but not streamed",
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        expect(events).toHaveLength(2);
        const recon = events[1];
        expect(recon.role).toBe("reconciliation");
        expect(recon.inputTokens).toBe(512);
        expect(recon.cacheWriteTokens).toBe(1000);
        expect(recon.cacheReadTokens).toBe(0);
        expect(recon.note).toBe("billed but not streamed");
        // It carries no generation/branch identity.
        expect(recon.generationMs).toBeNull();
        expect(recon.parentToolUseId).toBeNull();
        expect(recon.toolUses).toEqual([]);
    });

    test("prices the reconciliation row from the turn model (Bedrock open-weight: static rates are authoritative)", () => {
        // Bedrock open-weight runs on fixed Bedrock rates (like Claude) with no
        // OpenRouter actual-cost capture, so static pricing IS correct here and the
        // reconciliation row must carry it (else the Cost column under-sums).
        const turns: TurnEntry[] = [
            {
                model_used: "zai.glm-5",
                messages: [
                    {
                        role: "assistant",
                        model: "zai.glm-5",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                        input_tokens: 0,
                        output_tokens: 1000,
                        cache_read_tokens: 0,
                    },
                    {
                        role: "reconciliation",
                        input_tokens: 100000,
                        output_tokens: 0,
                        cache_creation_tokens: 0,
                        cache_read_tokens: 300000,
                        note: "billed but not streamed",
                    },
                ],
            },
        ];
        const events = parseMessages(turns);
        const [asst, recon] = events;
        // zai.glm-5: in 1.2 / out 3.84 / cacheRead 0 per MTok.
        // reconciliation = 100000*1.2 + 300000*0 = 120000 tok-$ / 1e6 = 0.12.
        expect(recon.model).toBe("zai.glm-5");
        expect(recon.costUsd).toBeCloseTo(0.12, 9);
        expect((asst.costUsd ?? 0) + (recon.costUsd ?? 0)).toBeCloseTo(0.12384, 9);
    });

    test("does NOT statically price OpenRouter models (their cost is the captured actual)", () => {
        // OpenRouter routes per-request, so a static headline rate is wrong; the
        // evalboard deliberately leaves these unpriced (→ "—") and shows the real
        // per-call cost in the per-call table (ProviderCallTableSection) instead.
        const turns: TurnEntry[] = [
            {
                model_used: "deepseek/deepseek-v4-pro",
                messages: [
                    {
                        role: "assistant",
                        model: "deepseek/deepseek-v4-pro",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                        input_tokens: 0,
                        output_tokens: 1000,
                        cache_read_tokens: 0,
                    },
                    {
                        role: "reconciliation",
                        input_tokens: 100000,
                        output_tokens: 0,
                        cache_read_tokens: 300000,
                        note: "billed but not streamed",
                    },
                ],
            },
        ];
        const [asst, recon] = parseMessages(turns);
        expect(asst.costUsd).toBeNull(); // no static price for OpenRouter
        expect(recon.costUsd).toBeNull();
    });

    test("reconciliation cost stays null when the turn model is absent (legacy runs)", () => {
        const turns: TurnEntry[] = [
            {
                messages: [
                    {
                        role: "reconciliation",
                        input_tokens: 512,
                        cache_creation_tokens: 1000,
                        note: "no model",
                    },
                ],
            },
        ];
        const recon = parseMessages(turns)[0];
        expect(recon.model).toBeNull();
        expect(recon.costUsd).toBeNull();
    });
});

describe("parseMessages — open-weight cost apportionment", () => {
    // An assistant message carrying token buckets + a model, on a turn that
    // reports a real per-turn total_cost_usd. Mirrors the Pi harness shape:
    // real stream cost at the turn, models absent from the rate card.
    function tokMsg(
        model: string,
        input: number,
        output: number,
        // Distinct started_at + message_id so separate generations are NOT
        // collapsed into one emission row by the same-emission heuristic.
        startSec = 0,
        messageId?: string,
    ) {
        const ss = String(startSec).padStart(2, "0");
        return {
            role: "assistant",
            started_at: `2026-01-01T00:00:${ss}.000Z`,
            completed_at: `2026-01-01T00:00:${ss}.500Z`,
            generation_duration_ms: 500,
            content_blocks: [{ block_type: "text" as const, text: "x" }],
            input_tokens: input,
            output_tokens: output,
            model,
            message_id: messageId ?? `m-${startSec}`,
        };
    }

    test("unpriced model + real turn cost: rows are filled and sum EXACTLY to the total", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "openrouter/unpriced-xyz",
                token_usage: { total_cost_usd: 0.009 },
                messages: [
                    tokMsg("openrouter/unpriced-xyz", 300, 100, 0), // weight 400
                    tokMsg("openrouter/unpriced-xyz", 100, 0, 2), //   weight 100
                ],
            },
        ];
        const rows = parseMessages(turns);
        expect(rows).toHaveLength(2);
        for (const r of rows) expect(r.costUsd).not.toBeNull();
        const sum = rows.reduce((a, r) => a + (r.costUsd ?? 0), 0);
        expect(sum).toBeCloseTo(0.009, 10); // exact to the cent and far beyond
        // Apportioned by token share: first row 400/500, second 100/500.
        expect(rows[0].costUsd).toBeCloseTo(0.009 * (400 / 500), 10);
    });

    test("provider_call_costs present (LiteLLM/OpenCode): NOT apportioned — cost lives in the ProviderCall table", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "openrouter/unpriced-xyz",
                token_usage: { total_cost_usd: 0.009 },
                // The open-weight actual-cost join already booked this turn's real
                // cost per-call; apportioning the turn total onto message rows too
                // would surface the same money twice.
                provider_call_costs: [{ cost_usd: 0.009, input_tokens: 300, output_tokens: 100 }],
                messages: [tokMsg("openrouter/unpriced-xyz", 300, 100, 0)],
            },
        ];
        // Unpriced model + a per-call audit → the row stays blank, NOT apportioned.
        expect(parseMessages(turns)[0].costUsd).toBeNull();
    });

    test("unpriced model + NO turn cost: rows stay blank (—), not a misleading $0", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "openrouter/unpriced-xyz",
                messages: [tokMsg("openrouter/unpriced-xyz", 300, 100)],
            },
        ];
        expect(parseMessages(turns)[0].costUsd).toBeNull();
    });

    test("priced model is left on the rate card (apportionment never overrides)", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "gpt-5.6-terra",
                token_usage: { total_cost_usd: 999 }, // absurd; must NOT leak into rows
                messages: [tokMsg("gpt-5.6-terra", 1_000_000, 1_000_000)],
            },
        ];
        const r = parseMessages(turns)[0];
        // Rate card: 1M input @ $2 + 1M output @ $12 = $14, not the bogus 999.
        expect(r.costUsd).toBeCloseTo(14, 6);
    });
});

// A raw with MULTIPLE block kinds in one message entry — the Delegate shape.
// 93% of its emissions are mixed (142/174 are thinking+tool_use), which the
// old priority chain booked entirely to whichever kind it tested first.
function mixedMsg(opts: {
    startedAt: string;
    completedAt: string;
    genMs: number | null;
    outputTokens?: number | null;
    thinking?: string;
    text?: string;
    toolParams?: Record<string, unknown> | null;
}) {
    const blocks: Record<string, unknown>[] = [];
    if (opts.thinking !== undefined) {
        blocks.push({ block_type: "thinking", thinking: opts.thinking });
    }
    if (opts.text !== undefined) {
        blocks.push({ block_type: "text", text: opts.text });
    }
    if (opts.toolParams !== undefined && opts.toolParams !== null) {
        blocks.push({ block_type: "tool_use", tool_use_id: "tu_1" });
    }
    return {
        role: "assistant",
        started_at: opts.startedAt,
        completed_at: opts.completedAt,
        generation_duration_ms: opts.genMs,
        output_tokens: opts.outputTokens ?? null,
        content_blocks: blocks,
    };
}

function toolCmd(params: Record<string, unknown>, toolId = "tu_1") {
    // `tool_id`, not `tool_use_id`: that is the key parseMessages resolves a
    // block against. With the wrong field the command never resolves, params
    // fall back to {}, and EVERY tool weighs exactly 1 — which silently makes
    // a content-size split test assert nothing.
    return {
        tool_id: toolId,
        tool_name: "Bash",
        parameters: params,
        result_status: "success",
    };
}

describe("kindWeights", () => {
    test("units are consistent: chars are converted, tool proxies are not", () => {
        // 400 chars / 4 = 100 tokens, weighed against a 100-token tool proxy.
        const w = kindWeights(400, 0, [100]);
        expect(w.thinking).toBe(100);
        expect(w.tool).toBe(100);
        expect(w.text).toBe(0);
    });

    test("total is the sum of its parts", () => {
        const w = kindWeights(400, 80, [10, 5]);
        expect(w.total).toBe(w.thinking + w.tool + w.text);
        expect(w.total).toBe(100 + 20 + 15);
    });

    test("no content at all weighs nothing", () => {
        expect(kindWeights(0, 0, []).total).toBe(0);
    });
});

describe("parseMessages — mixed-kind emissions", () => {
    // Every case here asserts BOTH invariants, so a change that fixes one
    // side and breaks the other cannot pass.
    //
    // TIME is always exact: mixedGenMs is the bucket for whatever no kind
    // could claim, so the four parts reconstruct generationMs.
    //
    // OUTPUT has no mixed bucket, by design — an emission with nothing
    // sizeable attributes its output to no kind rather than inventing a
    // fourth output figure. So the parts may only UNDER-sum, never over,
    // and `exactOutput` asks for equality wherever content existed. It is
    // the over-sum that was the bug: the same tokens counted twice.
    function assertSums(
        e: ReturnType<typeof parseMessages>[number],
        { exactOutput = true }: { exactOutput?: boolean } = {},
    ) {
        if (e.generationMs != null) {
            expect(
                (e.thinkingMs ?? 0) +
                    (e.textMs ?? 0) +
                    (e.toolGenMs ?? 0) +
                    (e.mixedGenMs ?? 0),
            ).toBeCloseTo(e.generationMs, 6);
        }
        if (e.outputTokens != null) {
            const attributed =
                (e.thinkingOutputTokens ?? 0) +
                (e.textOutputTokens ?? 0) +
                e.toolUses.reduce((a, t) => a + (t.outputTokens ?? 0), 0);
            expect(attributed).toBeLessThanOrEqual(e.outputTokens);
            if (exactOutput) expect(attributed).toBe(e.outputTokens);
        }
    }

    test("thinking + tool_use splits BOTH time and output by content size", () => {
        // The 142-of-174 Delegate case. Thinking text is 400 chars (100
        // proxy-tokens); the tool's params are sized to match, so the split
        // is roughly even — and crucially neither bucket gets 0 or all.
        const params = { command: "x".repeat(396) };
        const events = parseMessages([
            {
                messages: [
                    mixedMsg({
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:10.000Z",
                        genMs: 10_000,
                        outputTokens: 1000,
                        thinking: "t".repeat(400),
                        toolParams: params,
                    }),
                ],
                commands: [toolCmd(params)],
            },
        ]);
        expect(events).toHaveLength(1);
        const e = events[0];

        // The command resolved, so the tool really is weighed by its args.
        expect(e.toolUses[0].toolName).toBe("Bash");
        // 400 thinking chars -> 100 proxy-tokens; the params serialize to
        // ~102. Neither ~0% nor ~100%: today's bug reported 99.8% thinking,
        // and the `outputTokens - toolWeight` trap would report 100% tool.
        expect(e.thinkingMs).toBeGreaterThan(4_000);
        expect(e.thinkingMs).toBeLessThan(6_000);
        expect(e.toolGenMs).toBeGreaterThan(4_000);
        expect(e.toolGenMs).toBeLessThan(6_000);
        expect(e.mixedGenMs).toBeNull();
        // The double-count: 1000 output tokens used to be attributed to
        // thinking AND to the tool, so this summed to 2000.
        expect(
            (e.thinkingOutputTokens ?? 0) + (e.toolUses[0].outputTokens ?? 0),
        ).toBe(1000);
        // Per-tool generation time is the tool SHARE, not the whole emission
        // — a tool row must not out-report the tool total above it. Within a
        // millisecond: the group total is rounded to integer ms, the per-tool
        // figure is not.
        expect(Math.abs((e.toolUses[0].genMs ?? 0) - (e.toolGenMs ?? 0))).toBeLessThan(1);
        // The bug this replaces: the tool row carried the WHOLE emission.
        expect(e.toolUses[0].genMs).toBeLessThan(6_000);
        assertSums(e);
    });

    test("neither invariant over-sums across a rounding sweep", () => {
        // The tie case: two Math.round calls on shares that add to the total
        // can each round up. Booking the tool share in the first pass and the
        // rest via splitByWeight made that reachable, so sweep it.
        for (const thinkChars of [100, 397, 400, 401, 800, 1201, 2000]) {
            for (const proxyChars of [96, 200, 396, 404, 800]) {
                for (const out of [613, 999, 1000, 1001]) {
                    const params = { command: "z".repeat(proxyChars) };
                    const [e] = parseMessages([
                        {
                            messages: [
                                mixedMsg({
                                    startedAt: "2026-01-01T00:00:00.000Z",
                                    completedAt: "2026-01-01T00:00:10.000Z",
                                    genMs: 10_000,
                                    outputTokens: out,
                                    thinking: "t".repeat(thinkChars),
                                    toolParams: params,
                                }),
                            ],
                            commands: [toolCmd(params)],
                        },
                    ]);
                    const attributed =
                        (e.thinkingOutputTokens ?? 0) +
                        (e.textOutputTokens ?? 0) +
                        e.toolUses.reduce((a, t) => a + (t.outputTokens ?? 0), 0);
                    expect(attributed).toBe(out);
                    expect(
                        (e.thinkingMs ?? 0) +
                            (e.textMs ?? 0) +
                            (e.toolGenMs ?? 0) +
                            (e.mixedGenMs ?? 0),
                    ).toBeCloseTo(10_000, 6);
                }
            }
        }
    });

    test("text + thinking + tool_use splits three ways and sums exactly", () => {
        const params = { command: "y".repeat(200) };
        const events = parseMessages([
            {
                messages: [
                    mixedMsg({
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:09.000Z",
                        genMs: 9000,
                        outputTokens: 999,
                        thinking: "t".repeat(400),
                        text: "x".repeat(120),
                        toolParams: params,
                    }),
                ],
                commands: [toolCmd(params)],
            },
        ]);
        const e = events[0];
        expect(e.thinkingMs).toBeGreaterThan(0);
        expect(e.textMs).toBeGreaterThan(0);
        expect(e.toolGenMs).toBeGreaterThan(0);
        assertSums(e);
    });

    test("empty thinking text reads as all-tool, not as unattributable", () => {
        // Some harnesses hide CoT. "We have no thinking content to size" is
        // an honest all-tool reading; mixedGenMs is for having NOTHING.
        const params = { command: "z".repeat(200) };
        const events = parseMessages([
            {
                messages: [
                    mixedMsg({
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:04.000Z",
                        genMs: 4000,
                        outputTokens: 400,
                        thinking: "",
                        toolParams: params,
                    }),
                ],
                commands: [toolCmd(params)],
            },
        ]);
        const e = events[0];
        expect(e.toolGenMs).toBe(4000);
        expect(e.thinkingMs).toBeNull();
        expect(e.mixedGenMs).toBeNull();
        assertSums(e);
    });

    test("a mixed emission with no sizeable content lands in mixed", () => {
        // Previously this fell through all three chain branches and was
        // silently dropped from the breakdown while still counting in genSum.
        const events = parseMessages([
            {
                messages: [
                    mixedMsg({
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:00.500Z",
                        genMs: 500,
                        outputTokens: 10,
                        thinking: "",
                        text: "",
                    }),
                ],
            },
        ]);
        const e = events[0];
        expect(e.mixedGenMs).toBe(500);
        expect(e.thinkingMs).toBeNull();
        expect(e.textMs).toBeNull();
        // Nothing was sizeable, so the output belongs to no kind either.
        expect(e.thinkingOutputTokens ?? 0).toBe(0);
        expect(e.textOutputTokens ?? 0).toBe(0);
        assertSums(e, { exactOutput: false });
    });

    test("a measured zero does not invent a mixed bucket", () => {
        const events = parseMessages([
            {
                messages: [
                    mixedMsg({
                        startedAt: "2026-01-01T00:00:00.000Z",
                        completedAt: "2026-01-01T00:00:00.000Z",
                        genMs: 0,
                        outputTokens: 10,
                        thinking: "",
                        text: "",
                    }),
                ],
            },
        ]);
        expect(events[0].mixedGenMs).toBeNull();
    });

    test("a repeated single kind is still single-kind", () => {
        // ['tool_use','tool_use'] is ONE kind — compare distinct kinds, not
        // array length, or two parallel calls read as a mixed emission.
        const params = { command: "ls" };
        const entry = mixedMsg({
            startedAt: "2026-01-01T00:00:00.000Z",
            completedAt: "2026-01-01T00:00:03.000Z",
            genMs: 3000,
            outputTokens: 60,
            toolParams: params,
        });
        entry.content_blocks.push({ block_type: "tool_use", tool_use_id: "tu_2" });
        const events = parseMessages([
            {
                messages: [entry],
                commands: [toolCmd(params), toolCmd(params, "tu_2")],
            },
        ]);
        const e = events[0];
        expect(e.toolGenMs).toBe(3000);
        expect(e.mixedGenMs).toBeNull();
        assertSums(e);
    });

    test("Codex shape: two single-kind raws sharing a message_id", () => {
        const events = parseMessages([
            {
                messages: [
                    {
                        role: "assistant",
                        message_id: "m1",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:00.800Z",
                        generation_duration_ms: 800,
                        content_blocks: [{ block_type: "thinking", thinking: "plan" }],
                    },
                    {
                        role: "assistant",
                        message_id: "m1",
                        started_at: "2026-01-01T00:00:00.800Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 200,
                        content_blocks: [
                            { block_type: "tool_use", tool_use_id: "tu_1" },
                        ],
                    },
                ],
                commands: [toolCmd({ command: "ls" })],
            },
        ]);
        // They regroup into ONE MessageEvent, but each raw is single-kind, so
        // the breakdown is real rather than apportioned.
        expect(events).toHaveLength(1);
        const e = events[0];
        expect(e.thinkingMs).toBe(800);
        expect(e.toolGenMs).toBe(200);
        expect(e.mixedGenMs).toBeNull();
        assertSums(e);
    });
});

describe("parseMessages — tool execution bounds", () => {
    const turns = (cmd: Record<string, unknown>): TurnEntry[] => [
        {
            messages: [
                msg("tool_use", {
                    startedAt: "2026-01-01T00:00:00.000Z",
                    completedAt: "2026-01-01T00:00:00.500Z",
                    genMs: 500,
                    toolUseId: "tu_1",
                }),
            ],
            commands: [
                { tool_name: "Bash", tool_id: "tu_1", parameters: { command: "ls" }, ...cmd },
            ],
        },
    ];

    test("the recorded execution bounds reach the tool use as epoch ms", () => {
        // Without them the Unaccounted residual can only SUM overlapping
        // calls, which is what made it go negative on a concurrent-tool task.
        const [e] = parseMessages(
            turns({
                duration_ms: 1000,
                execution_started_at: "2026-01-01T00:00:01.000Z",
                execution_completed_at: "2026-01-01T00:00:02.000Z",
            }),
        );
        const t = e.toolUses[0];
        expect(t.execEndMs! - t.execStartMs!).toBe(1000);
        expect(t.execStartMs).toBe(Date.parse("2026-01-01T00:00:01.000Z"));
    });

    test("a command with no bounds keeps its duration and reports null bounds", () => {
        const [e] = parseMessages(turns({ duration_ms: 1000 }));
        expect(e.toolUses[0].durationMs).toBe(1000);
        expect(e.toolUses[0].execStartMs).toBeNull();
        expect(e.toolUses[0].execEndMs).toBeNull();
    });

    test("an unparseable stamp is null, not NaN", () => {
        const [e] = parseMessages(
            turns({ execution_started_at: "not a date", execution_completed_at: null }),
        );
        expect(e.toolUses[0].execStartMs).toBeNull();
        expect(e.toolUses[0].execEndMs).toBeNull();
    });
});
