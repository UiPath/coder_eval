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
