// Cascade-aware "what-if" model for thinking-token and tool-output cost.
//
// The per-task page exposes two independent 0–200% sliders: one over the
// agent's thinking volume, one over the size of tool results (what tools return
// into context). Scaling either doesn't just change one turn's bill — because
// every later API call re-reads the running transcript from the prompt cache,
// content produced early is paid for again (at the cache-read rate) on every
// subsequent call. This module turns the recorded per-message token stream into
// a projection that captures that cascade for both dimensions.
//
// THINKING vs TOOL OUTPUT — the two differ in which bills they touch:
//   • Thinking is *generated* by the model, so scaling it moves the output bill
//     at the call it's made, plus the cache write/read cascade afterwards.
//   • Tool results are *injected* (not model output), so scaling them touches
//     the cache write/read cascade only — never the output bill.
//
// Tool-result token volume is not recorded directly (only a 280-char preview),
// so it is ESTIMATED as cache growth unexplained by the model's own output: a
// result returned after call j is the new cache-written content on call j+1, so
//   toolResult_j ≈ cacheWrite_{j+1} − output_j   (clamped ≥ 0).
// This needs per-message tokens (coder_eval #336+); on older runs the tool lever
// has nothing to estimate and is disabled.
//
// Model (all per-call quantities indexed by emission j = 0..M-1, in order):
//
//   thinkTokens_j  estimated extended-thinking tokens generated at call j.
//                  reasoning_tokens when present (>0), else
//                  output_tokens_j × (thinkingMs_j / generationMs_j) — token
//                  generation time is ~linear in tokens, so the thinking-time
//                  share is a sound proxy for the thinking-token share.
//
//   Scaling thinking by s changes three billed quantities, each linear in s:
//     ΔOutput      = (s-1) · Σ thinkTokens_j
//                    (thinking is part of the output bill at the call it's made)
//     ΔCacheWrite  = (s-1) · Σ thinkTokens_j · writes_j
//                    (each block is written into the cache once, on the next call)
//     ΔCacheRead   = (s-1) · Σ thinkTokens_j · reads_j
//                    (then re-read on every call after that — the cascade)
//   where, for a run of M calls, a block made at call j is present in the
//   context of the M-1-j later calls: the first is the cache write, the rest
//   are cache reads → writes_j = (M-1-j ≥ 1 ? 1 : 0),
//   reads_j = max(0, M-1-j-1) = max(0, M-2-j).
//
// Held fixed ("assuming everything else is fine", per the feature request): the
// trajectory itself — number of calls, tool calls, tool-result sizes, and the
// genuinely-uncached input. We isolate the cost impact of thinking *volume*,
// not whether the agent would have taken a different path.
//
// Baseline token totals come from the authoritative iteration token_usage
// (tokens.* on TaskDetail); per-message tokens/timing are used only to
// apportion thinking across calls. At s=1 the projection reproduces the
// recorded token mix exactly.

import type { MessageEvent, TokenTotals } from "@/lib/runs";

// Per-million-token prices. Ported from src/coder_eval/proxy/pricing.py — keep
// in sync when that table changes. Source: Anthropic / OpenAI public pricing.
export interface Pricing {
    inputPerMTok: number;
    outputPerMTok: number;
    cacheWritePerMTok: number;
    cacheReadPerMTok: number;
}

const PRICING: Record<string, Pricing> = {
    "claude-opus-4-6": p(15, 75, 18.75, 1.5),
    "claude-opus-4-6-20250514": p(15, 75, 18.75, 1.5),
    "claude-opus-4-5-20251101": p(15, 75, 18.75, 1.5),
    "claude-opus-4-20250514": p(15, 75, 18.75, 1.5),
    "claude-sonnet-4-6": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-6-20250514": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-5-20250929": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-20250514": p(3, 15, 3.75, 0.3),
    "claude-haiku-4-5-20251001": p(0.8, 4, 1, 0.08),
    "claude-3-7-sonnet-20250219": p(3, 15, 3.75, 0.3),
    "claude-3-5-sonnet-20241022": p(3, 15, 3.75, 0.3),
    "claude-3-5-sonnet-20240620": p(3, 15, 3.75, 0.3),
    "claude-3-opus-20240229": p(15, 75, 18.75, 1.5),
    "claude-3-sonnet-20240229": p(3, 15, 3.75, 0.3),
    "claude-3-haiku-20240307": p(0.25, 1.25, 0.3, 0.03),
    "gpt-5-codex": p(1.25, 10, 1.25, 0.125),
    "gpt-5": p(1.25, 10, 1.25, 0.125),
    "gpt-5.3-codex": p(1.75, 14, 1.75, 0.175),
};

function p(
    input: number,
    output: number,
    cacheWrite: number,
    cacheRead: number,
): Pricing {
    return {
        inputPerMTok: input,
        outputPerMTok: output,
        cacheWritePerMTok: cacheWrite,
        cacheReadPerMTok: cacheRead,
    };
}

// Resolve pricing for a model id, tolerating undated aliases (the recorded
// model is usually the canonical id like "claude-sonnet-4-6", but be lenient
// about a trailing date suffix either way).
export function resolvePricing(model: string | null): Pricing | null {
    if (!model) return null;
    if (PRICING[model]) return PRICING[model];
    // Try stripping a trailing -YYYYMMDD date.
    const undated = model.replace(/-\d{8}$/, "");
    if (PRICING[undated]) return PRICING[undated];
    // Try a prefix match (handles e.g. a future dated alias we didn't list).
    for (const key of Object.keys(PRICING)) {
        if (model.startsWith(key) || key.startsWith(model)) return PRICING[key];
    }
    return null;
}

// Plain, serializable model handed from the server section to the client
// slider. No methods — projectThinking() below is the pure projector the
// client calls as the slider moves.
export interface ThinkingModel {
    model: string;
    pricing: Pricing;
    // Whether per-message tokens were available (true) or thinking was
    // apportioned from iteration totals by generation time (false). Surfaced
    // as a caveat in the UI.
    perMessageTokens: boolean;
    // Authoritative baseline token totals (iteration token_usage).
    inputTokens: number;
    outputTokens: number;
    cacheCreationTokens: number;
    cacheReadTokens: number;
    // Number of assistant emissions (≈ API calls) in the run.
    calls: number;
    // Estimated total thinking tokens generated across the run.
    thinkTokens: number;
    // Estimated total tool-result tokens injected across the run (cache growth
    // unexplained by model output — see header). 0 when per-message tokens are
    // absent, which disables the tool-output lever.
    toolResultTokens: number;
    // Recorded cost (sum of iteration total_cost_usd), for reference. May be
    // null on runs that didn't record it. The projection's own baseline is
    // computed from pricing and is what the slider moves around.
    recordedCostUsd: number | null;
    // Thinking cascade coefficients (per unit of (thinkScale - 1)). Thinking is
    // model output, so it moves the output bill AND the cache cascade.
    coeffOutput: number; // Σ thinkTokens_j               (= thinkTokens)
    coeffCacheWrite: number; // Σ thinkTokens_j · writes_j
    coeffCacheRead: number; // Σ thinkTokens_j · reads_j
    // Tool-output cascade coefficients (per unit of (toolScale - 1)). Tool
    // results are injected, not generated, so they move the cache cascade only —
    // there is no tool-output analogue of coeffOutput.
    coeffToolWrite: number; // Σ toolResult_j · writes_j     (= toolResultTokens)
    coeffToolRead: number; // Σ toolResult_j · reads_j
}

export interface ThinkingProjection {
    scale: number;
    inputTokens: number;
    outputTokens: number;
    cacheCreationTokens: number;
    cacheReadTokens: number;
    totalTokens: number;
    costInput: number;
    costOutput: number;
    costCacheWrite: number;
    costCacheRead: number;
    costUsd: number;
}

function clampNonNeg(n: number): number {
    return n < 0 ? 0 : n;
}

// Build the (serializable) projection model for a task. Returns null when we
// can't price the run (unknown model) — the caller renders a graceful note.
export function buildThinkingModel(
    messages: MessageEvent[],
    tokens: TokenTotals,
    recordedCostUsd: number | null,
): ThinkingModel | null {
    const calls = messages.length;
    if (calls === 0) return null;

    // Per-message output present iff the summed per-message output is non-zero.
    const perMessageOutputSum = messages.reduce(
        (s, m) => s + (m.outputTokens ?? 0),
        0,
    );
    const perMessageTokens = perMessageOutputSum > 0;

    // Resolve a single primary model to price with: the one carrying the most
    // output (the main agent model, not background haiku compaction calls).
    const modelOut = new Map<string, number>();
    const totalGen = messages.reduce((s, m) => s + (m.generationMs ?? 0), 0);
    for (const m of messages) {
        // Weight by generation time, not output_tokens: the CLI's per-message
        // output is unreliable (see thinking estimate below), but generation
        // time is recorded for every emission.
        const w = m.generationMs ?? 0;
        if (m.model) modelOut.set(m.model, (modelOut.get(m.model) ?? 0) + w);
    }
    let primaryModel: string | null = null;
    let best = -1;
    for (const [name, w] of modelOut) {
        if (w > best) {
            best = w;
            primaryModel = name;
        }
    }
    const pricing = resolvePricing(primaryModel);
    if (!pricing || !primaryModel) return null;

    const OUT = tokens.output;

    // Per-call thinking tokens, in order.
    //
    // Thinking tokens are billed inside output_tokens, but the SDK never breaks
    // them out (reasoning_tokens is ~always 0) and the CLI's *per-message*
    // output_tokens is unreliable for the thinking emission specifically — it
    // routinely records a handful of tokens for multi-second thinking blocks
    // (e.g. cd_ls_smoke: 8 tokens for 5.4 s of thinking). What *is* reliable is
    // generation time. Token generation runs at a ~constant rate, so a call's
    // thinking-time share of the run is a sound proxy for its share of the
    // (authoritative, iteration-level) total output tokens:
    //
    //     thinkTokens_j = OUT_total · (thinkingMs_j / Σ generationMs)
    //
    // reasoning_tokens still wins when a future SDK actually populates it.
    const thinkPerCall: number[] = messages.map((m) => {
        if (m.reasoningTokens != null && m.reasoningTokens > 0) {
            return m.reasoningTokens;
        }
        const think = m.thinkingMs ?? 0;
        if (totalGen <= 0 || think <= 0) return 0;
        return OUT * (think / totalGen);
    });

    // Per-call tool-result tokens, in order. Not recorded directly, so we infer
    // them from cache growth call j's own output doesn't explain: the result
    // returned after call j is the new content cache-written at call j+1, so
    //   toolResult_j ≈ cacheWrite_{j+1} − realOutput_j   (clamped ≥ 0).
    //
    // realOutput_j is NOT the recorded per-message output: that figure badly
    // under-counts thinking emissions (a multi-second thinking block routinely
    // logs a handful of tokens — see above), so subtracting it would leave the
    // missing thinking tokens in cacheWrite_{j+1} to be misread as tool results.
    // Those tokens would then be scaled by BOTH levers — the thinking lever via
    // its gen-time estimate AND the tool lever — double-counting them. So we
    // subtract the SAME thinking estimate the thinking lever uses, plus the
    // reliable non-thinking part of recorded output:
    //   realOutput_j = thinkTokens_j + max(0, output_j − thinkingOutput_j)
    // which keeps the two levers operating on disjoint token populations.
    //
    // Defined only for j ≤ calls-2 (the last call has no successor to write its
    // results). Needs per-message cacheWrite; absent → all zero, which disables
    // the tool lever.
    const toolPerCall: number[] = messages.map((m, j) => {
        const next = messages[j + 1];
        if (!next || next.cacheWriteTokens == null) return 0;
        const nonThinkingOut = Math.max(
            0,
            (m.outputTokens ?? 0) - (m.thinkingOutputTokens ?? 0),
        );
        const realOutput = thinkPerCall[j] + nonThinkingOut;
        return Math.max(0, next.cacheWriteTokens - realOutput);
    });

    // Cascade coefficients. Content present at call j is in the context of the
    // M-1-j later calls: first is a cache write, the rest cache reads.
    //   • Thinking made AT call j cascades over calls j+1 .. M-1.
    //   • A tool result returned after call j first appears at call j+1, so it
    //     cascades over calls j+1 .. M-1 too (toolPerCall already shifts the
    //     measurement to the successor; positionally it lands the same way).
    let coeffOutput = 0;
    let coeffCacheWrite = 0;
    let coeffCacheRead = 0;
    let coeffToolWrite = 0;
    let coeffToolRead = 0;
    for (let j = 0; j < calls; j++) {
        const later = calls - 1 - j;
        const t = thinkPerCall[j];
        if (t > 0) {
            coeffOutput += t;
            coeffCacheWrite += t * (later >= 1 ? 1 : 0);
            coeffCacheRead += t * Math.max(0, later - 1);
        }
        const tr = toolPerCall[j];
        if (tr > 0) {
            // The result is written at call j+1 then re-read on calls j+2 .. M-1
            // → write once (always, since toolPerCall[j]=0 when no successor),
            // re-read on the remaining `later - 1` calls.
            coeffToolWrite += tr;
            coeffToolRead += tr * Math.max(0, later - 1);
        }
    }

    return {
        model: primaryModel,
        pricing,
        perMessageTokens,
        inputTokens: tokens.input,
        outputTokens: tokens.output,
        cacheCreationTokens: tokens.cacheCreation,
        cacheReadTokens: tokens.cacheRead,
        calls,
        thinkTokens: coeffOutput,
        toolResultTokens: coeffToolWrite,
        recordedCostUsd,
        coeffOutput,
        coeffCacheWrite,
        coeffCacheRead,
        coeffToolWrite,
        coeffToolRead,
    };
}

// Project token mix + cost at a given thinking + tool-output scale (1 = as-run,
// 0 = none, 2 = 200%). The two scales are independent: thinking moves output +
// its cache cascade; tool output moves the cache cascade only. Both deltas are
// added on top of the authoritative baseline, so (1, 1) reproduces the recorded
// mix exactly. Pure; safe to call on every slider tick.
export function projectThinking(
    m: ThinkingModel,
    scale: number,
    toolScale = 1,
): ThinkingProjection {
    const d = scale - 1;
    const dt = toolScale - 1;
    const outputTokens = clampNonNeg(m.outputTokens + d * m.coeffOutput);
    const cacheCreationTokens = clampNonNeg(
        m.cacheCreationTokens + d * m.coeffCacheWrite + dt * m.coeffToolWrite,
    );
    const cacheReadTokens = clampNonNeg(
        m.cacheReadTokens + d * m.coeffCacheRead + dt * m.coeffToolRead,
    );
    const inputTokens = m.inputTokens;

    const costInput = (inputTokens * m.pricing.inputPerMTok) / 1e6;
    const costOutput = (outputTokens * m.pricing.outputPerMTok) / 1e6;
    const costCacheWrite =
        (cacheCreationTokens * m.pricing.cacheWritePerMTok) / 1e6;
    const costCacheRead = (cacheReadTokens * m.pricing.cacheReadPerMTok) / 1e6;

    return {
        scale,
        inputTokens,
        outputTokens,
        cacheCreationTokens,
        cacheReadTokens,
        totalTokens:
            inputTokens + outputTokens + cacheCreationTokens + cacheReadTokens,
        costInput,
        costOutput,
        costCacheWrite,
        costCacheRead,
        costUsd: costInput + costOutput + costCacheWrite + costCacheRead,
    };
}

// True (cascade-aware) marginal cost of one thinking token across the whole
// run, expressed as a multiple of its face output price. Quantifies "early
// thinking is re-read from cache on every later call". Returns null when the
// run generated ~no thinking.
export function thinkingAmplification(m: ThinkingModel): number | null {
    if (m.coeffOutput <= 0) return null;
    const perTokenUsd =
        (m.coeffOutput * m.pricing.outputPerMTok +
            m.coeffCacheWrite * m.pricing.cacheWritePerMTok +
            m.coeffCacheRead * m.pricing.cacheReadPerMTok) /
        1e6 /
        m.coeffOutput;
    const faceUsd = m.pricing.outputPerMTok / 1e6;
    return faceUsd > 0 ? perTokenUsd / faceUsd : null;
}

// Cascade amplification for tool-result tokens: their lifetime cache cost
// (written once + re-read on every later call) as a multiple of the one-time
// cache-write price. Quantifies "a big early tool result is re-read all run".
// Returns null when the run injected ~no measurable tool-result tokens.
export function toolAmplification(m: ThinkingModel): number | null {
    if (m.coeffToolWrite <= 0) return null;
    const perTokenUsd =
        (m.coeffToolWrite * m.pricing.cacheWritePerMTok +
            m.coeffToolRead * m.pricing.cacheReadPerMTok) /
        1e6 /
        m.coeffToolWrite;
    const faceUsd = m.pricing.cacheWritePerMTok / 1e6;
    return faceUsd > 0 ? perTokenUsd / faceUsd : null;
}
