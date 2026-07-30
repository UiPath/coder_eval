import { describe, expect, test } from "vitest";
import { parseMessages, type TurnEntry } from "../runs";

// Per-message cost is now ALWAYS the static rate-card estimate — actual per-call
// cost is no longer distributed onto transcript messages (it lives in the
// separate providerCalls table). task.json carries no `cost_usd` on messages, so
// parseMessages must price each message purely from its token buckets + model.
describe("parseMessages: message cost is the rate-card estimate", () => {
    test("prices a message from the rate card (no cost_usd field present)", () => {
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

    test("leaves an unpriced (OpenRouter) model at null — no inline actual cost", () => {
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
                    },
                ],
            },
        ];
        const [asst] = parseMessages(turns);
        // OpenRouter models are intentionally unpriced in the rate card → null.
        expect(asst.costUsd).toBeNull();
    });
});
