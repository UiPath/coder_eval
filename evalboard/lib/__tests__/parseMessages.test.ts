import { describe, expect, test } from "vitest";
import { approxTokens, parseMessages, type TurnEntry } from "../runs";

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
});
