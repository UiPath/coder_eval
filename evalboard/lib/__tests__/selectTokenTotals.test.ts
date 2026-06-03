import { describe, expect, test } from "vitest";
import {
    type MessageEvent,
    type TurnEntry,
    selectTokenTotals,
} from "../runs";

// Minimal MessageEvent carrying only the token fields selectTokenTotals reads.
// The per-message stream from parseMessages is already deduped per message_id,
// so summing it is the per-request billing ground truth.
function msg(tokens: {
    input?: number;
    output?: number;
    cacheWrite?: number;
    cacheRead?: number;
}): MessageEvent {
    return {
        inputTokens: tokens.input ?? 0,
        outputTokens: tokens.output ?? 0,
        cacheWriteTokens: tokens.cacheWrite ?? 0,
        cacheReadTokens: tokens.cacheRead ?? 0,
    } as MessageEvent;
}

describe("selectTokenTotals", () => {
    test("prefers per-message sums over the iteration ResultMessage snapshot", () => {
        // The bug: iteration token_usage (the SDK ResultMessage snapshot) under-
        // reports cache_read on a multi-call run. The per-message stream sums to
        // the true cumulative.
        const messages = [
            msg({ input: 10, output: 100, cacheRead: 20_000 }),
            msg({ input: 5, output: 80, cacheRead: 40_000 }),
            msg({ input: 3, output: 60, cacheRead: 60_000 }),
        ];
        const turns: TurnEntry[] = [
            {
                token_usage: {
                    input_tokens: 3,
                    output_tokens: 240,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 60_000, // snapshot — far below the 120k sum
                },
            },
        ];

        const tokens = selectTokenTotals(messages, turns);
        expect(tokens.cacheRead).toBe(120_000); // 20k + 40k + 60k, not 60k
        expect(tokens.input).toBe(18);
        expect(tokens.output).toBe(240);
        expect(tokens.total).toBe(120_000 + 18 + 240);
    });

    test("falls back to iteration token_usage when the stream has no token data", () => {
        // Legacy runs predating per-message tokens: every MessageEvent token
        // field is 0, so the stream total is 0 and we use the iteration aggregate.
        const messages = [msg({}), msg({})];
        const turns: TurnEntry[] = [
            {
                token_usage: {
                    input_tokens: 1000,
                    output_tokens: 500,
                    cache_creation_input_tokens: 200,
                    cache_read_input_tokens: 100,
                },
            },
        ];

        const tokens = selectTokenTotals(messages, turns);
        expect(tokens.input).toBe(1000);
        expect(tokens.output).toBe(500);
        expect(tokens.cacheCreation).toBe(200);
        expect(tokens.cacheRead).toBe(100);
    });

    test("returns zeros when neither source has data", () => {
        const tokens = selectTokenTotals([], []);
        expect(tokens.total).toBe(0);
    });

    test("prefers iteration token_usage when it is the more complete source", () => {
        // Current runs build iteration token_usage from the SDK model_usage —
        // cumulative, and larger than the per-message stream because it includes
        // sub-agent cache-creation the stream doesn't bubble up. The larger
        // (more complete) total wins.
        const messages = [
            msg({ input: 110, output: 1800, cacheWrite: 21873, cacheRead: 41844 }),
        ];
        const turns: TurnEntry[] = [
            {
                token_usage: {
                    input_tokens: 834,
                    output_tokens: 1834,
                    cache_creation_input_tokens: 51339, // model_usage cumulative
                    cache_read_input_tokens: 41844,
                },
            },
        ];
        const tokens = selectTokenTotals(messages, turns);
        expect(tokens.cacheCreation).toBe(51339); // iteration wins, not 21873
        expect(tokens.input).toBe(834);
    });
});
