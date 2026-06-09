"""Telemetry and command statistics models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, computed_field, model_validator


class TokenUsage(BaseModel):
    """Token usage from a single agent turn or aggregated across turns.

    The prompt is split into three mutually-exclusive buckets that SUM to the
    full prompt: ``uncached_input_tokens`` (fresh, billed at the input rate),
    ``cache_creation_input_tokens`` (written to cache), and
    ``cache_read_input_tokens`` (served from cache). ``input_tokens`` is the
    DERIVED total of the three (the whole prompt the model processed), exposed
    as a computed field so it can never drift from its parts.

    Provider mapping for ``uncached_input_tokens``:

    * Anthropic / Claude: the API ``input_tokens`` (the uncached slice);
      ``cache_creation``/``cache_read`` as reported.
    * OpenAI / Codex: ``input - cached`` (the fresh slice). ``cache_creation`` is
      0 — OpenAI bills no separate cache-write fee — and ``cache_read`` is the
      cached prefix.

    COST bills ``uncached_input_tokens`` at the input rate, NOT ``input_tokens``
    (which already contains the cache buckets and would double-count them).
    """

    @model_validator(mode="before")
    @classmethod
    def _adopt_legacy_input_tokens(cls, data: Any) -> Any:
        """Read pre-split ``task.json`` files without silently under-counting.

        Before the uncached/derived split, the stored field was ``input_tokens``
        and it carried the FULL prompt. It is now a computed field, so a naive
        reload drops it and recomputes from the cache buckets only — turning a
        stored ``input_tokens=1000`` into ``350``. When the new stored field
        ``uncached_input_tokens`` is absent but a legacy ``input_tokens`` is
        present, adopt the legacy value as ``uncached_input_tokens`` so the Python
        read path agrees with the evalboard (which already falls back the same
        way). Current-format payloads carry ``uncached_input_tokens`` and are
        untouched.
        """
        if isinstance(data, dict) and "uncached_input_tokens" not in data and "input_tokens" in data:
            data = {**data, "uncached_input_tokens": data["input_tokens"]}
        return data

    uncached_input_tokens: int = Field(
        default=0,
        description=(
            "Fresh (uncached) input prompt tokens, billed at the input rate. Maps to "
            "Anthropic's API input_tokens; for Codex == (input - cached). This is the "
            "field cost is computed from, NOT input_tokens (the derived total)."
        ),
    )
    output_tokens: int = Field(default=0, description="Output completion tokens")
    cache_creation_input_tokens: int = Field(
        default=0,
        description="Tokens written to the prompt cache this call (Anthropic only; 0 for Codex).",
    )
    cache_read_input_tokens: int = Field(default=0, description="Tokens read from prompt cache")
    total_cost_usd: float | None = Field(default=None, description="Total cost in USD (from SDK)")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def input_tokens(self) -> int:
        """Total prompt tokens = uncached + cache_creation + cache_read.

        Derived (read-only) so it always equals the sum of its parts. This is the
        whole prompt the model processed, NOT the billable-at-input-rate slice —
        for cost use ``uncached_input_tokens``.
        """
        return self.uncached_input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def total_tokens(self) -> int:
        """All tokens the turn processed: full prompt (input_tokens) + output."""
        return self.input_tokens + self.output_tokens

    def is_empty(self) -> bool:
        """True when every token counter is zero (cost is ignored).

        Used to decide whether a proxy delta or SDK-parsed usage carries any
        real traffic before attributing it to a turn/judge.
        """
        return (
            self.uncached_input_tokens == 0
            and self.output_tokens == 0
            and self.cache_creation_input_tokens == 0
            and self.cache_read_input_tokens == 0
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Field-wise sum. Cost is summed only over the non-None operands
        (stays None when neither carries a cost). ``input_tokens`` is derived, so
        summing the parts keeps the total consistent automatically."""
        costs = [c for c in (self.total_cost_usd, other.total_cost_usd) if c is not None]
        return TokenUsage(
            uncached_input_tokens=self.uncached_input_tokens + other.uncached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            total_cost_usd=sum(costs) if costs else None,
        )


class ContentBlock(BaseModel):
    """One content block within a message, in emission order.

    Can be a text block (user/assistant), thinking block (assistant),
    tool_use block (assistant), or tool_result block (user).
    """

    block_type: Literal["text", "thinking", "tool_use", "tool_result"] = Field(
        description="Block kind: text, thinking, tool_use, or tool_result."
    )
    sequence: int = Field(description="0-indexed position within the parent message.content_blocks list.")

    text: str | None = Field(default=None, description="Text content. Set when block_type='text' or 'tool_result'.")

    thinking: str | None = Field(
        default=None,
        description="Extended-thinking reasoning content. Set when block_type='thinking'.",
    )
    signature: str | None = Field(default=None, description="Anthropic-signed signature for the thinking block.")

    tool_use_id: str | None = Field(
        default=None,
        description="Tool invocation ID; joins to CommandTelemetry. Set for tool_use or tool_result blocks.",
    )
    is_error: bool = Field(
        default=False,
        description="True if tool_result block represents an error.",
    )


class UserMessage(BaseModel):
    """One user-side message (text or tool result) within a TurnRecord.

    For simulator-driven dialog mode: captures user utterances from the
    simulator plus orchestrator-side wall-clock timing. Populated in
    simulation runs only.

    For tool results: captured when a tool_result UserMessage arrives
    from the SDK, containing tool result blocks and error status.
    """

    role: Literal["user"] = "user"

    text: str = Field(description="Utterance sent to the agent (stop token stripped).")
    raw_text: str | None = Field(
        default=None,
        description="Simulator's raw output before stop-token stripping. None for pinned initial_prompt openers.",
    )
    stop_requested: bool | None = Field(
        default=None, description="Whether the simulator emitted its stop token. None for pinned openers."
    )

    started_at: datetime | None = Field(
        default=None, description="Wall-clock start of the simulator LLM call. None for pinned openers."
    )
    completed_at: datetime | None = Field(
        default=None, description="Wall-clock end of the simulator LLM call. None for pinned openers."
    )
    generation_duration_ms: float | None = Field(
        default=None, description="completed_at - started_at in milliseconds. None for pinned openers."
    )

    input_tokens: int = Field(default=0, description="Simulator prompt tokens (0 if SDK did not surface them).")
    output_tokens: int = Field(default=0, description="Simulator completion tokens (0 if SDK did not surface them).")
    model: str | None = Field(default=None, description="Simulator model identifier.")
    simulator_failed: bool = Field(
        default=False, description="True if the simulator call raised; text/raw_text reflect the fallback or are empty."
    )


class AssistantMessage(BaseModel):
    """One LLM response message (from SDK AssistantMessage) within a TurnRecord.

    Captures per-message generation timing, content blocks, and token usage so
    LLM time can be measured directly rather than inferred from gaps between
    command timestamps. Per-message token usage is per-generation. The run's
    authoritative cumulative billing comes from the SDK ResultMessage
    ``model_usage`` (cumulative per-model, reconciles to ``total_cost_usd``);
    summing this stream is only a fallback when ``model_usage`` is absent (e.g.
    LLMGW/proxy). The stream under-reports sub-agent cache-creation/input — those
    emissions are only partially bubbled up — so do NOT treat it as the source of
    truth for run-level cost.
    """

    role: Literal["assistant"] = "assistant"

    started_at: datetime = Field(description="Wall-clock start of generation (end of the previous SDK event).")
    completed_at: datetime = Field(description="Wall-clock arrival of the AssistantMessage from the SDK.")
    generation_duration_ms: float = Field(
        description="completed_at - started_at in milliseconds (wall-clock between SDK events).",
    )

    content_blocks: list[ContentBlock] = Field(
        default_factory=list,
        description="Content blocks Claude emitted in this turn, in emission order.",
    )
    tool_use_ids: list[str] = Field(
        default_factory=list,
        description="tool_use_id values from content_blocks, for joining with CommandTelemetry and tool_result blocks.",
    )

    input_tokens: int = Field(
        default=0,
        description=(
            "Input prompt tokens for this LLM call. Zero on follow-up emissions that "
            "share a message_id with an earlier AssistantMessage (the CLI splits one "
            "API call into multiple emissions); billing is recorded on the first only. "
            "Per-call only; the run's cumulative input is taken from ResultMessage "
            "model_usage (summing this stream is a fallback — it misses sub-agent input)."
        ),
    )
    output_tokens: int = Field(
        default=0,
        description=(
            "Output tokens generated in this LLM call. Recovered from the "
            "message_delta stream event when include_partial_messages is on; falls "
            "back to the (often partial) SDK assistant-event value otherwise. Per-call "
            "only; the run's cumulative output is taken from ResultMessage model_usage "
            "(summing this stream is a fallback)."
        ),
    )
    cache_creation_tokens: int = Field(default=0, description="Tokens used to create prompt cache for this call.")
    cache_read_tokens: int = Field(default=0, description="Tokens read from prompt cache for this call.")
    reasoning_tokens: int = Field(default=0, description="Extended-thinking tokens; subset of output_tokens.")

    stop_reason: str | None = Field(default=None, description="SDK stop reason: 'tool_use', 'end_turn', etc.")
    model: str | None = Field(default=None, description="Model identifier that generated this turn.")

    message_id: str | None = Field(
        default=None,
        description=(
            "Anthropic API message_id. Multiple AssistantMessage records can share this id "
            "when the Claude Code CLI splits one API response into per-block-kind events. "
            "Downstream tooling can group by this id to recover one logical generation."
        ),
    )

    parent_tool_use_id: str | None = Field(
        default=None,
        description=(
            "The Task tool_use_id that spawned this message's sub-agent, or null for the "
            "main agent thread. Sub-agent (Task tool) calls bubble up into the same message "
            "stream, but each sub-agent has its OWN context that is not re-read by the main "
            "thread or sibling sub-agents — so the conversation is a tree, not a flat list. "
            "Grouping by this id recovers the branch a call belongs to, which is required to "
            "model the prompt-cache re-read cascade correctly (content cascades only within "
            "its own branch). Sourced from the SDK AssistantMessage.parent_tool_use_id."
        ),
    )


class ReconciliationMessage(BaseModel):
    """A synthetic transcript entry carrying tokens the agent billed but never
    surfaced as a streamed generation.

    The authoritative turn total (Claude's ``ResultMessage.model_usage``, Codex's
    thread total + folded sub-agent tokens) is consistently LARGER than the sum
    of the per-``AssistantMessage`` token buckets, for reasons that are
    architectural, not bugs: a fixed prompt slice (~512 input tokens on Claude)
    is billed but rides on no SDK-emitted message, and sub-agent input/cache is
    only partially bubbled into the parent stream. Rather than fabricate
    per-generation numbers (which would match no real call), the residual is
    booked once, explicitly, as this entry — so that summing the transcript's
    token buckets EXACTLY reproduces the authoritative ``token_usage`` for the
    turn. Consumers that sum the stream (the evalboard) therefore reconcile to
    the bill without a separate aggregate; this entry is the visible "missing
    tokens" line. Carries no cost (cost stays on the authoritative aggregate) and
    is never an LLM generation — it is excluded from generation/turn counts.
    """

    role: Literal["reconciliation"] = "reconciliation"

    input_tokens: int = Field(
        default=0,
        description="Residual uncached input tokens (authoritative turn total minus the per-message sum).",
    )
    output_tokens: int = Field(default=0, description="Residual output tokens.")
    cache_creation_tokens: int = Field(default=0, description="Residual cache-creation tokens.")
    cache_read_tokens: int = Field(default=0, description="Residual cache-read tokens.")

    note: str = Field(
        default="",
        description="Human-readable explanation of why these tokens are unattributed (shown in the UI).",
    )


class CommandTelemetry(BaseModel):
    """Telemetry for a single command execution.

    Captures detailed information about each tool use by the agent,
    enabling analysis, debugging, and optimization.

    **Deprecation notice:** This model duplicates data already available in
    TurnRecord.messages (as tool_use and tool_result blocks). In future versions,
    command telemetry should be derived from the messages array rather than
    maintained as a parallel structure. Kept for backward compatibility.
    """

    # Identity
    tool_name: str = Field(description="Tool name (Read, Write, Bash, Edit, Glob, etc.)")
    tool_id: str = Field(description="Unique ID from Claude SDK for this tool invocation")

    assistant_turn_index: int | None = Field(
        default=None,
        description=(
            "0-indexed position among AssistantMessage entries only. "
            "Filter turn_record.messages by role='assistant' before indexing."
        ),
    )

    # Timing: generation_completed_at is when Claude finished emitting the
    # tool_use block; execution_* bracket the actual tool run. `timestamp`
    # equals generation_completed_at and `duration_ms` equals
    # execution_completed_at - execution_started_at; both are retained for
    # downstream consumers that index on the original field names.
    timestamp: datetime = Field(description="Equal to generation_completed_at; when the tool_use block arrived.")
    duration_ms: float | None = Field(
        default=None,
        description="Command execution time in milliseconds (None = not complete, set in two-phase processing).",
    )
    generation_completed_at: datetime | None = Field(
        default=None,
        description="When Claude finished emitting the tool_use block.",
    )
    execution_started_at: datetime | None = Field(default=None, description="When tool execution began.")
    execution_completed_at: datetime | None = Field(
        default=None, description="When tool execution completed (success or error)."
    )

    # Parameters (structured)
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Structured command parameters (e.g., {'file_path': 'main.py'} for Read)"
    )

    # Results
    result_status: Literal["success", "error", "unknown"] | None = Field(
        default=None,
        description="Whether the command succeeded or failed (None = pending result, set during two-phase processing)",
    )
    result_summary: str | None = Field(
        default=None,
        description=(
            "Full tool-result content as text (untruncated). Despite the name, this is "
            "the complete result body, not a preview — kept whole so sub-agent returns "
            "and other large payloads are preserved."
        ),
    )
    error_message: str | None = Field(default=None, description="Error message if command failed")
    result_data: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description=(
            "First parsed JSON object or array found in the tool result content. "
            "Prefix noise (e.g. CLI warning lines before the JSON body) and trailing "
            "garbage are tolerated. None for content without any parseable JSON object "
            "or array and for bare primitives (strings, numbers, booleans, null). "
            "Complements result_summary (a short text preview) by preserving the full "
            "structured payload so downstream consumers can extract specialised views "
            "without re-parsing the log."
        ),
    )

    # Metadata
    sequence_number: int = Field(default=0, description="Order within the turn (0-indexed)")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def result_tokens(self) -> int:
        """Approximate token size of the tool result the model received.

        Derived from the untruncated ``result_summary`` content (≈4 chars/token,
        matching the evalboard heuristic), so it is a DIRECT, cache-independent
        measure of "tool output size" — available identically whether prompt
        caching was on or off. The cost simulator uses this instead of inferring
        result size from prompt-cache growth (which is unavailable when caching is
        disabled). Approximate, not the API's exact tokenizer count, but
        deterministic and always present. 0 when the tool returned no content.
        """
        if not self.result_summary:
            return 0
        return -(-len(self.result_summary) // 4)  # ceil(len / 4)


#: One entry in a turn's per-message transcript, discriminated on ``role``.
#: ``ReconciliationMessage`` is the synthetic "missing tokens" line that makes the
#: transcript's token buckets sum to the authoritative turn total. Defined once
#: here and reused by ``TurnRecord.messages`` and ``AgentEndEvent.messages`` so the
#: list element type is identical everywhere (avoids list-invariance friction).
TranscriptMessage = Annotated[UserMessage | AssistantMessage | ReconciliationMessage, Discriminator("role")]


class SlowestCommandInfo(BaseModel):
    """Information about a slow command for performance analysis.

    Type-safe model for reporting slowest commands in statistics.
    """

    tool: str = Field(description="Tool name (e.g., 'Bash', 'Read')")
    duration_ms: float = Field(description="Execution duration in milliseconds")
    parameters: dict[str, Any] = Field(description="Command parameters")
    tool_id: str | None = Field(default=None, description="Optional: Unique tool invocation ID")


class CommandStatistics(BaseModel):
    """Aggregated statistics for command usage in an evaluation.

    Provides summary metrics for analysis and reporting.
    """

    total_commands: int = Field(description="Total number of commands executed")
    commands_by_tool: dict[str, int] = Field(
        default_factory=dict, description="Count of commands per tool (e.g., {'Bash': 45, 'Read': 12})"
    )

    # Timing
    total_command_time_ms: float = Field(default=0.0, description="Total time spent executing commands (milliseconds)")
    avg_command_time_ms: float | None = Field(default=None, description="Average command execution time")
    slowest_commands: list[SlowestCommandInfo] = Field(
        default_factory=list, description="Top 5 slowest commands with details (type-safe model)"
    )

    # Success/Failure
    successful_commands: int = Field(default=0, description="Commands that succeeded")
    failed_commands: int = Field(default=0, description="Commands that failed")
    unknown_commands: int = Field(
        default=0,
        description="Commands with unknown status (missing ResultMessage, indicates agent/SDK interruption)",
    )
    success_rate: float = Field(
        default=0.0,
        description="Percentage of known commands that succeeded: success / (success + failed), excludes unknown",
    )

    # Patterns
    most_common_sequence: str | None = Field(
        default=None, description="Most common 3-command sequence (e.g., 'Read -> Edit -> Bash')"
    )
