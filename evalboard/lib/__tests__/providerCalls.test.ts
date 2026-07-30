import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { parseProviderCalls, type TurnEntry } from "../runs";

// parseProviderCalls maps each turn's snake-case provider_call_costs audit rows
// into the camelCase per-call table shape, keyed by iteration, skipping turns
// with no rows.
describe("parseProviderCalls", () => {
    test("maps rows and skips empty turns", () => {
        const turns: TurnEntry[] = [
            { provider_call_costs: [] }, // skipped
            {
                provider_call_costs: [
                    {
                        call_id: "call-abc",
                        provider: "openrouter",
                        cost_usd: 0.0021,
                        input_tokens: 715,
                        cache_read_tokens: 4096,
                        cache_write_tokens: 0,
                        output_tokens: 8,
                    },
                ],
            },
        ];
        const out = parseProviderCalls(turns);
        expect(out).toHaveLength(1);
        expect(out[0].iteration).toBe(1);
        expect(out[0].calls[0]).toEqual({
            callId: "call-abc",
            provider: "openrouter",
            costUsd: 0.0021,
            inputTokens: 715,
            cacheReadTokens: 4096,
            cacheWriteTokens: 0,
            outputTokens: 8,
        });
    });

    test("is empty when no turn carries provider_call_costs", () => {
        expect(parseProviderCalls([{ messages: [] }])).toEqual([]);
    });

    test("nulls absent fields", () => {
        const out = parseProviderCalls([
            { provider_call_costs: [{ cost_usd: 0.5 }] },
        ]);
        expect(out[0].calls[0]).toEqual({
            callId: null,
            provider: null,
            costUsd: 0.5,
            inputTokens: null,
            cacheReadTokens: null,
            cacheWriteTokens: null,
            outputTokens: null,
        });
    });
});

// End-to-end: a TaskDetail parsed off disk exposes providerCalls from the turn's
// provider_call_costs. Mirrors collect.test.ts's env-stub + fresh-import pattern
// so RUNS_DIR resolves to a throwaway dir.
const RUN = "2026-01-01_00-00-00";
const TASK = "demo-task";
let tmp: string;

async function write(rel: string, body: string): Promise<void> {
    const abs = path.join(tmp, rel);
    await fs.mkdir(path.dirname(abs), { recursive: true });
    await fs.writeFile(abs, body);
}

async function loadRuns() {
    vi.resetModules();
    vi.stubEnv("EVALBOARD_LOCAL_RUNS_DIR", tmp);
    return import("../runs");
}

beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), "evalboard-provcalls-"));
    await write(
        `${RUN}/run.json`,
        JSON.stringify({
            run_id: RUN,
            task_results: [{ task_id: TASK, status: "success" }],
        }),
    );
    await write(
        `${RUN}/default/${TASK}/00/task.json`,
        JSON.stringify({
            final_status: "success",
            iterations: [
                {
                    model_used: "deepseek/deepseek-v4-pro",
                    provider_call_costs: [
                        {
                            call_id: "call-1",
                            provider: "openrouter",
                            cost_usd: 0.0012,
                            input_tokens: 100,
                            cache_read_tokens: 200,
                            cache_write_tokens: 50,
                            output_tokens: 25,
                        },
                        {
                            call_id: "call-2",
                            provider: "openrouter",
                            cost_usd: 0.0034,
                            input_tokens: 300,
                            cache_read_tokens: 0,
                            cache_write_tokens: 0,
                            output_tokens: 40,
                        },
                    ],
                },
            ],
        }),
    );
});

afterEach(async () => {
    vi.unstubAllEnvs();
    await fs.rm(tmp, { recursive: true, force: true });
});

describe("readTaskDetail: providerCalls", () => {
    test("exposes the per-call rows from provider_call_costs", async () => {
        const { readTaskDetail } = await loadRuns();
        const detail = await readTaskDetail(RUN, TASK);
        expect(detail).not.toBeNull();
        expect(detail?.providerCalls).toHaveLength(1);
        const turn = detail!.providerCalls[0];
        expect(turn.iteration).toBe(0);
        expect(turn.calls).toHaveLength(2);
        expect(turn.calls[0]).toEqual({
            callId: "call-1",
            provider: "openrouter",
            costUsd: 0.0012,
            inputTokens: 100,
            cacheReadTokens: 200,
            cacheWriteTokens: 50,
            outputTokens: 25,
        });
        expect(turn.calls[1].callId).toBe("call-2");
        expect(turn.calls[1].costUsd).toBe(0.0034);
    });
});
