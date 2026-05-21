# Typed verdict channel for judge criteria

**Date:** 2026-05-20
**Status:** Phase 2-3 wired; phase 5 burn-in pending; phase 6 cleanup deferred.

## What it is

`agent_judge` and `llm_judge` now report their verdict via a structured
`submit_verdict` tool call rather than a JSON-in-text payload that the criterion
re-parses with regex + brace walking.

The tool exists in three native shapes — one per backend:

- **SDK MCP** — an in-process MCP server (`coder_eval_judge`) holding a
  closure-bound `submit_verdict` tool that writes the verdict into a
  `VerdictCapture` the criterion reads after the agent loop terminates.
  Used by `agent_judge`. Forced into `allowed_tools` as
  `mcp__coder_eval_judge__submit_verdict`.
- **LangChain** — `.bind_tools([SUBMIT_VERDICT_LC_TOOL], tool_choice="submit_verdict")`
  forces the model to emit a `submit_verdict` call as its sole response.
  Used by `llm_judge` on LLMGW routes.
- **Anthropic-native** — the Bedrock httpx-direct call body gains
  `"tools": [SUBMIT_VERDICT_ANTHROPIC_TOOL]` and a `tool_choice` directive.
  Used by `llm_judge` on Bedrock and Direct/Proxy routes.

All three converge on `JudgeVerdict.model_validate(args_dict)` —
the single validation anchor.

## Choosing a channel

```yaml
- type: agent_judge
  prompt: "Grade..."
  verdict_channel: tool   # default

- type: llm_judge
  prompt: "Grade..."
  verdict_channel: text   # legacy parser, removed after burn-in
```

Default: `"tool"`. Set `verdict_channel: text` to opt back into the legacy
JSON-in-text parser during the burn-in phase.

## SDK vs LangChain/Bedrock robustness asymmetry

LangChain and Bedrock both support `tool_choice="submit_verdict"` (or its
native equivalent), which *forces* the model to call the tool. The SDK
(`agent_judge`) has no equivalent — the judge runs a multi-turn agent loop
and the system prompt is the only mechanism telling it to call
`submit_verdict`. A judge that ignores the instruction surfaces as a
`"Judge did not call submit_verdict"` diagnostic with `score=0.0`. Phase 5
burn-in measures the actual rate.

## Burn-in telemetry

Each `JudgeCriterionResult` carries `channel_used: "tool" | "text" | None`
indicating which channel produced the result (or `None` if the criterion
was skipped). The field is read during the phase-5 burn-in to confirm the
tool channel is reliable across backends before the text path is deleted.

## Removal path

The `verdict_channel` field and the text-channel code are scheduled for
removal once burn-in shows the tool channel is reliable. The plan at
`c/2026-05-20-judge-typed-verdict-channel.md` (phase 6) describes the
deletion sequence; a `field_validator` will reject YAML that still sets
`verdict_channel`, with a one-line migration message.

## Burn-in results

**Date:** 2026-05-21.
**Script:** `tmp/burn_in/smoke.py` (not committed; ad-hoc).
**Model:** `anthropic.claude-sonnet-4-6` for both LLM judges; `claude-haiku-4-5-20251001` for the SDK judge.
**Task:** trivial one-file grading (``hello.txt`` starts with "Hello").

| Route | Result | Score | Notes |
|---|---|---|---|
| `agent_judge` / Claude Code SDK | ✅ PASS | 1.0 | `submit_verdict` MCP tool fired end-to-end; capture populated; verdict surfaces via `extract_verdict_from_sdk_messages`. |
| `llm_judge` / Bedrock (`us-east-2`) | ✅ PASS | 1.0 | Forced `tool_choice` + Anthropic-native tool spec; response walked by `extract_verdict_from_bedrock_response`. |
| `llm_judge` / LLMGW | ⚠️ blocked on creds | — | OAuth2 token exchange returned `invalid_client` from `alpha.uipath.com` — environment-level credential issue, not a code defect. Unit tests with `_StubLLMWithToolCall` cover the `.bind_tools()` → `.tool_calls` extraction path, and the same `JudgeVerdict.model_validate` anchor that Bedrock exercised is used here. Re-run after refreshing `LLMGW_CLIENT_ID` / `LLMGW_CLIENT_SECRET` to fully close phase 5. |

**Go/no-go:** Conditional GO. The SDK and Bedrock paths are validated end-to-end. The LLMGW path is structurally validated via the same shared `extract_verdict_from_*` plumbing and unit tests; one live re-run with valid `alpha.uipath.com` credentials would fully close the burn-in.
