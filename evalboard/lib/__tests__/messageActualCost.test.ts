import { describe, expect, test } from "vitest";
import { parseMessages, type TurnEntry } from "../runs";

// The Python actual-cost join writes real per-call cost + cache onto each raw
// message (open-weight backend). parseMessages must surface that INLINE in the
// timeline — preferring it over the static rate-card estimate — and drain the
// reconciliation row. On other backends (no cost_usd) it falls back to the rate card.
describe("parseMessages: actual per-call cost inline in the timeline", () => {
    test("uses the joined actual cost + real cache read when present (open-weight)", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "deepseek/deepseek-v4-pro",
                messages: [
                    {
                        role: "assistant",
                        message_id: "m1",
                        model: "deepseek/deepseek-v4-pro",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                        input_tokens: 715,
                        cache_read_tokens: 4096,
                        output_tokens: 8,
                        cost_usd: 0.0022,
                    },
                    { role: "reconciliation", input_tokens: 0, cache_read_tokens: 0, cost_usd: 0 },
                ],
            },
        ];
        const [asst, recon] = parseMessages(turns);
        expect(asst.costUsd).toBe(0.0022); // actual, not the unpriced → null static estimate
        expect(asst.cacheReadTokens).toBe(4096); // real per-call cache read, shown per row
        expect(recon.costUsd).toBe(0); // reconciliation drained
    });

    test("falls back to static rate-card pricing when no actual cost is present (Claude)", () => {
        const turns: TurnEntry[] = [
            {
                model_used: "claude-sonnet-4-6",
                messages: [
                    {
                        role: "assistant",
                        message_id: "m1",
                        model: "claude-sonnet-4-6",
                        started_at: "2026-01-01T00:00:00.000Z",
                        completed_at: "2026-01-01T00:00:01.000Z",
                        generation_duration_ms: 1000,
                        content_blocks: [{ block_type: "text", text: "hi" }],
                        input_tokens: 1000,
                        output_tokens: 2000,
                    },
                ],
            },
        ];
        const [asst] = parseMessages(turns);
        // claude-sonnet-4-6: input 3 / output 15 per MTok → (1000*3 + 2000*15)/1e6 = 0.033
        expect(asst.costUsd).toBeCloseTo(0.033, 9);
    });
});
