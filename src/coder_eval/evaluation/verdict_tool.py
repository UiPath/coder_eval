"""Typed verdict tool channel — replaces text-with-JSON parsing for judge criteria.

Defines a single ``submit_verdict`` tool the judge calls as its terminal action,
exposed in two native shapes (SDK MCP, Anthropic) so each judge backend can use
its own tool-calling protocol. The extractors then collapse those native
responses into a single ``tuple[JudgeVerdict | None, str | None]`` contract so
the rest of the criterion code doesn't branch on backend.

The shared anchor is ``JudgeVerdict.model_validate(args_dict)``: every backend
ultimately delivers a ``dict``-shaped tool input, and that one call decides
validity. Error vocabulary is preserved via ``_format_validation_error`` so
existing tests / on-disk records keep matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from pydantic import ValidationError

from coder_eval.models import JudgeVerdict


SUBMIT_VERDICT_TOOL_NAME = "submit_verdict"
"""Single source of truth for the tool name across all three backends."""

SUBMIT_VERDICT_MCP_TOOL_NAME = "mcp__coder_eval_judge__submit_verdict"
"""SDK ``allowed_tools`` entry — must match the SDK's ``mcp__<server>__<tool>`` convention."""

_SUBMIT_VERDICT_DESCRIPTION = (
    "Submit your final score, rationale, and findings. Call exactly once as your terminal action."
)


SUBMIT_VERDICT_ANTHROPIC_TOOL: dict[str, Any] = {
    "name": SUBMIT_VERDICT_TOOL_NAME,
    "description": _SUBMIT_VERDICT_DESCRIPTION,
    "input_schema": JudgeVerdict.model_json_schema(),
}
"""Anthropic-native tool spec for the Bedrock httpx2-direct path and Anthropic SDK calls.

Note: Anthropic uses ``input_schema``, not OpenAI's ``parameters``.
"""


@dataclass
class VerdictCapture:
    """Closure-side state shared between the SDK ``@tool`` and the criterion caller.

    The SDK tool runs inside the agent's MCP server but writes through to this
    plain object so the criterion can read the verdict after the agent loop
    finishes. Each judge invocation owns a fresh ``VerdictCapture`` — no global
    state, safe under concurrent judge runs.
    """

    verdict: JudgeVerdict | None = None
    error: str | None = None
    called_count: int = 0
    # LAST observed call wins: a successful retry overwrites the previous verdict,
    # and an invalid retry clears the prior valid verdict (and sets ``error``).
    # The criterion reads the final state via ``extract_verdict_from_capture``.


def _build_submit_verdict_tool(capture: VerdictCapture) -> SdkMcpTool[Any]:
    """Construct the ``@tool``-decorated submit_verdict closure bound to ``capture``.

    Kept as a separate helper so tests can drive the underlying handler directly
    (``SdkMcpTool.handler``) without going through the MCP Server's JSONRPC layer.
    """

    @tool(
        SUBMIT_VERDICT_TOOL_NAME,
        _SUBMIT_VERDICT_DESCRIPTION,
        JudgeVerdict.model_json_schema(),
    )
    async def submit_verdict(args: dict[str, Any]) -> dict[str, Any]:
        capture.called_count += 1
        try:
            verdict = JudgeVerdict.model_validate(args)
        except ValidationError as e:
            # LAST-call discipline: overwrite verdict too — a successful first
            # call followed by an invalid retry should surface the latest state.
            capture.verdict = None
            capture.error = _format_validation_error(e)
            return {"content": [{"type": "text", "text": f"Invalid: {capture.error}. Retry."}]}
        capture.verdict = verdict
        capture.error = None
        return {"content": [{"type": "text", "text": "Verdict received. Stop here."}]}

    return submit_verdict


def build_submit_verdict_mcp_server(capture: VerdictCapture) -> tuple[str, McpSdkServerConfig]:
    """Build the ``submit_verdict`` SDK MCP server bound to ``capture``.

    Returns ``(server_name, McpSdkServerConfig)`` ready to plug into
    ``ClaudeAgentOptions.mcp_servers``. The server name is
    ``"coder_eval_judge"`` and the resulting allowed-tool entry is
    ``SUBMIT_VERDICT_MCP_TOOL_NAME`` (the SDK exposes MCP tools as
    ``mcp__<server>__<tool>``).
    """
    sdk_tool = _build_submit_verdict_tool(capture)
    server = create_sdk_mcp_server("coder_eval_judge", "1.0.0", [sdk_tool])
    return "coder_eval_judge", server


def extract_verdict_from_capture(
    capture: VerdictCapture,
) -> tuple[JudgeVerdict | None, str | None]:
    """Read the LAST successful verdict from ``capture``.

    Returns ``(verdict, None)`` on success, ``(None, error)`` when the tool
    was called with invalid args, and ``(None, "Judge did not call submit_verdict")``
    when the tool was never called at all.
    """
    if capture.verdict is not None:
        return capture.verdict, None
    if capture.error is not None:
        return None, capture.error
    return None, "Judge did not call submit_verdict"


def extract_verdict_from_anthropic_response(
    response: dict[str, Any],
) -> tuple[JudgeVerdict | None, str | None]:
    """Read the LAST ``submit_verdict`` tool_use block from an Anthropic-shaped response.

    Walks ``response["content"]`` for blocks matching
    ``{"type": "tool_use", "name": "submit_verdict"}`` and validates the last
    one's ``input`` against ``JudgeVerdict``. Used by:

    * The Bedrock httpx2 path (``invoke_bedrock_judge_async``) — raw JSON dict.
    * The Anthropic SDK Direct path (``invoke_anthropic_judge_async``) —
      response converted via ``Message.model_dump()``.

    Both backends share Anthropic's native message shape.

    LAST-call discipline + non-dict guard: a final ``submit_verdict`` block
    whose ``input`` is missing or not a dict is treated as an invalid-args
    failure, not "did not call".
    """
    blocks = response.get("content") or []
    saw_submit_verdict = False
    last_input: dict[str, Any] | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != SUBMIT_VERDICT_TOOL_NAME:
            continue
        saw_submit_verdict = True
        block_input = block.get("input")
        last_input = block_input if isinstance(block_input, dict) else None
    if not saw_submit_verdict:
        return None, "Judge did not call submit_verdict"
    if last_input is None:
        return None, "submit_verdict input must be an object"
    try:
        return JudgeVerdict.model_validate(last_input), None
    except ValidationError as e:
        return None, _format_validation_error(e)


def _format_validation_error(e: ValidationError) -> str:
    """Normalize a Pydantic ``ValidationError`` into the legacy vocabulary.

    Preserves the strings produced by the old ``_verdict_shaped_error`` so
    persisted ``task.json`` records and existing test assertions stay stable:
      * ``score field missing in judge verdict``
      * ``score field is not a number: <repr>``
      * ``score field is not a finite number: <repr>``
      * ``rationale field must be a string, got <type>``
      * ``rationale field is empty after whitespace collapse``

    Multiple errors join with ``"; "``.
    """
    messages: list[str] = []
    for err in e.errors():
        loc = err.get("loc", ())
        field = loc[0] if loc else ""
        etype = err.get("type", "")
        msg = err.get("msg", "")

        if etype == "missing":
            # Generic on ``field`` so any future required ``JudgeVerdict`` field gets
            # the legacy "<name> field missing in judge verdict" vocabulary. Today
            # ``score`` is the only required field; the test suite pins the string.
            messages.append(f"{field or 'unknown'} field missing in judge verdict")
            continue

        # Pydantic wraps custom ValueErrors as "Value error, <body>".
        body = msg[len("Value error, ") :] if msg.startswith("Value error, ") else msg

        if field == "score" and body.startswith("score is not a number:"):
            messages.append("score field is not a number:" + body[len("score is not a number:") :])
            continue
        if field == "score" and body.startswith("score is not finite:"):
            messages.append("score field is not a finite number:" + body[len("score is not finite:") :])
            continue
        if field == "rationale" and body.startswith("rationale must be a string,"):
            messages.append("rationale field must be a string," + body[len("rationale must be a string,") :])
            continue
        if field == "rationale" and body == "rationale is empty after whitespace collapse":
            messages.append("rationale field is empty after whitespace collapse")
            continue

        # Fallback for any new error class we haven't mapped — surface the field qualifier
        # so the diagnostic still names the offending key.
        messages.append(f"{field} field: {body}" if field else body)

    return "; ".join(messages)
