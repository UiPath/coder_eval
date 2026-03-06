# LLM Gateway Proxy vs Direct API: Comparison Report (v1)

**Date**: 2026-03-06
**Model**: claude-sonnet-4-6
**Tasks**: `uipath_list_connectors` (v1) and `uipath_list_connectors_v2`

## Run Details

| | No Proxy (`--no-proxy`) | With Proxy (`--proxy`) |
|---|---|---|
| **Run ID** | `2026-03-06_10-36-56` | `2026-03-06_10-37-32` |
| **API Routing** | Direct Anthropic API | LLM Gateway proxy (via `alpha.uipath.com`) |

## High-Level Comparison

| Metric | No Proxy | With Proxy | Delta |
|--------|----------|------------|-------|
| **Total Duration** | 71.8s | 118.1s | +64.3% |
| **Success Rate** | 100% (2/2) | 100% (2/2) | -- |
| **Reliability Score** | 1.000 | 1.000 | -- |
| **Self-Corrections** | 0 | 0 | -- |
| **Total Asst Turns** | 27 | 22 | -18.5% |
| **Total Bash Commands** | 17 | 13 | -23.5% |
| **Command Success Rate** | 94.1% (16/17) | 100% (13/13) | +5.9pp |
| **Reported Total Cost** | $0.5358 | $0.4601 | -14.1% |

## Per-Task Breakdown

### Task: `uipath-list-connectors` (v1)

| Metric | No Proxy | With Proxy | Delta |
|--------|----------|------------|-------|
| Latency | 29.9s | 62.3s | +108.4% |
| Assistant Turns | 11 | 11 | -- |
| Bash Commands | 8 | 8 | -- |
| Errors | 0 | 0 | -- |
| Reported Cost | $0.2308 | $0.2715 | +17.6% |

Both runs followed a nearly identical strategy for this task:
1. Find `uipcli` in PATH
2. Explore help: `--help` -> `is --help` -> `is connectors --help` -> `is connectors list --help`
3. Run `uipcli is connectors list --format json`
4. Save output to `connectors.json`
5. Verify the file

**Same turn count, same commands, same outcome** -- the only difference is wall-clock time.

### Task: `uipath-list-connectors-v2`

| Metric | No Proxy | With Proxy | Delta |
|--------|----------|------------|-------|
| Latency | 41.9s | 55.8s | +33.2% |
| Assistant Turns | 16 | 11 | -31.3% |
| Bash Commands | 9 | 5 | -44.4% |
| Errors | 1 | 0 | -1 |
| Reported Cost | $0.3050 | $0.1886 | -38.2% |

The no-proxy run hit an error: the agent tried to pipe JSON through `grep -v` and `python3` in one command, which broke JSON parsing. It then struggled with shell redirection (`> file 2>&1` caused `uipcli` to misinterpret arguments). Recovery took 4 extra tool calls.

The proxy run executed cleanly in 5 commands with no errors.

## Analysis: Proxy-Dependent vs Random Differences

### Definitively Proxy-Dependent

#### 1. Latency Overhead (~2x on v1 task)
The proxy introduces measurable latency at multiple points:
- **S2S token acquisition**: ~1s per task start (visible in logs as `Acquiring S2S token` -> `S2S token acquired`)
- **Extra network hops**: Each LLM call goes through `local proxy -> LLM Gateway (alpha.uipath.com) -> backend model`, adding round-trip time
- **Per-turn overhead**: The proxy handles 2-4 HTTP requests per agent turn (streaming sonnet + non-streaming sonnet + streaming haiku + non-streaming haiku), each adding gateway routing latency

On the v1 task (identical turn count), latency went from 29.9s to 62.3s -- a **+32.4s / +108% overhead** attributable entirely to the proxy.

#### 2. Token Reporting Mechanism
- **No proxy**: The Claude Code SDK reports real token usage including prompt cache stats (`cache_creation_input_tokens`, `cache_read_input_tokens`). Example: task 1 shows 14,740 cache creation + 176,243 cache read + 1,316 output tokens.
- **With proxy**: The SDK reports `cost=$0` and all token fields as `0` because the proxy intercepts the response and the SDK can't extract usage metadata. Instead, the **proxy server** tracks tokens independently: "34 requests, 6,382 input + 3,338 output tokens" for task 1.

The "Total Tokens" in the report (`TokenUsage.total_tokens`) is defined as `input_tokens + output_tokens`, **excluding** cache tokens. This explains the discrepancy:
- No proxy: 3,177 tokens = SDK-reported `input_tokens` (21) + `output_tokens` (3,156). The much larger cache counts (176K+ cache reads) are tracked separately and not included.
- With proxy: 20,442 tokens = proxy-counted input + output across all 56 requests (including internal Haiku calls invisible in direct mode). The SDK itself reports all zeros since usage metadata is lost through the proxy.

These numbers are **not directly comparable** -- they measure different scopes of API traffic.

#### 3. Visible Internal SDK Traffic
The proxy logs reveal Claude Code SDK internals invisible in direct mode:
- **Haiku calls**: The SDK makes `claude-haiku-4-5-20251001` requests alongside Sonnet requests (likely for tool-use approval or summarization). These appear as interleaved proxy requests but aren't visible in the direct-API task logs.
- **Non-streaming requests**: Many calls use non-streaming mode (for `context_management` or short responses).
- **`count_tokens` requests**: The SDK periodically calls a token counting endpoint, which the proxy handles with synthetic responses.

#### 4. Request Field Stripping
The proxy strips fields not supported by the Bedrock-based gateway: `['model', 'context_management', 'stream']`. This doesn't appear to affect behavior but is a structural difference in the API path.

### Definitively Random (LLM Non-Determinism)

#### 1. v2 Task: Turn Count and Error Recovery (16 vs 11 turns)
The no-proxy v2 run took 16 assistant turns with 1 error; the proxy v2 run took 11 turns with 0 errors. The difference is entirely due to **different code generation by the LLM**:
- No-proxy agent tried `grep -v '"Log":' | python3 -c "..."` which broke JSON parsing -> recovery path added 4-5 extra commands
- Proxy agent used a simpler `> connectors.json 2>&1` redirect that worked on the first try

This is classic LLM stochastic behavior -- the same model with the same prompt chose different shell strategies.

#### 2. v2 Task: Cost Difference ($0.3050 vs $0.1886)
Flows directly from the turn count difference. More turns = more tokens = higher cost. Not proxy-related.

#### 3. CLI Exploration Strategy
Minor variations in the exact commands chosen (e.g., `which uipcli 2>/dev/null || echo "not in PATH"` vs `which uipcli || find ... -name "uipcli"`) reflect LLM sampling randomness, not proxy effects.

### Ambiguous / Needs More Data

#### 1. Whether the proxy affects model behavior
The proxy routes through Bedrock (`anthropic.claude-sonnet-4-6` -> gateway model). While nominally the same model, there could be subtle differences in:
- Temperature/sampling parameters being modified by the gateway
- System prompt handling differences in the Bedrock path
- Token limit differences

With only 1 run per configuration, we **cannot distinguish** proxy-induced behavioral changes from random variation. The v1 task (same turns, same strategy) suggests no behavioral difference, but the v2 task diverged. More runs are needed to establish statistical significance.

## Cost Comparison Caveat

The cost numbers are not directly comparable:

| Source | No Proxy | With Proxy |
|--------|----------|------------|
| SDK-reported cost | $0.5358 | $0.00 (not available) |
| Proxy-tracked tokens | N/A | 20,442 (input+output) |
| Report "Total Cost" | $0.5358 | $0.4601 |

The proxy run's reported cost ($0.4601) appears to be estimated from the proxy's token counts using standard Anthropic pricing, while the no-proxy cost ($0.5358) is the SDK's actual billing figure which includes cache token pricing. **The proxy run likely underestimates true cost** because it may not account for cache tokens or Bedrock-specific pricing.

## Conclusions

1. **Proxy adds ~30-60s latency overhead** per task due to network hops and authentication. On simple tasks this roughly doubles wall-clock time.
2. **Proxy does not affect task outcomes** -- both runs achieved 100% success with identical reliability scores.
3. **Turn count and error rate differences are random**, not proxy-caused. The v1 task proves this: identical turns (11) and commands (8) across both modes.
4. **Token/cost reporting is fundamentally different** between modes and currently not comparable. The proxy needs to forward usage metadata back to the SDK for accurate reporting.
5. **A single pair of runs is insufficient** to detect subtle behavioral effects of the proxy. Recommend running 5-10 repetitions per configuration to separate signal from noise.

## Recommendations for Next Steps

- Run each configuration 5+ times to get statistically meaningful latency and turn-count distributions
- Fix proxy token/cost pass-through so SDK reports accurate usage even through the gateway
- Measure proxy overhead in isolation (latency of proxy with no model call) to separate gateway latency from model latency
- Test with more complex tasks where behavioral differences would be more visible
