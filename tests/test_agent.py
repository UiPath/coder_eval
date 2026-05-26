"""Tests for the agent implementations."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ProcessError

from coder_eval.agent import AgentState
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.models import AgentConfig, AgentKind


def test_claude_agent_initialization():
    """Test that Claude agent can be initialized."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Bash"],
    )

    agent = ClaudeCodeAgent(config)

    assert agent.config == config
    assert agent.client is None
    assert agent.get_state() == AgentState.WORKING


def test_pending_turn_defaults_to_none():
    """Fresh agent has pending_turn = None (slot is empty at rest)."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)
    assert agent.pending_turn is None


@pytest.mark.asyncio
async def test_discard_pending_turn_clears_slot_and_decrements():
    """discard_pending_turn clears the slot and rolls back _iteration once."""
    from coder_eval.models import TurnRecord

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    partial = TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True)
    agent._iteration = 1
    agent.pending_turn = partial

    await agent.discard_pending_turn()

    assert agent.pending_turn is None
    assert agent._iteration == 0


@pytest.mark.asyncio
async def test_discard_pending_turn_idempotent():
    """discard_pending_turn is a no-op when pending_turn is already None."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    assert agent.pending_turn is None
    assert agent._iteration == 0

    # First call: nothing to discard — counter must not go negative.
    await agent.discard_pending_turn()
    assert agent.pending_turn is None
    assert agent._iteration == 0

    # Second call after a real discard: still a no-op.
    from coder_eval.models import TurnRecord

    partial = TurnRecord(iteration=2, user_input="p", agent_output="<partial>", crashed=True)
    agent._iteration = 2
    agent.pending_turn = partial
    await agent.discard_pending_turn()  # real discard
    await agent.discard_pending_turn()  # idempotent second call
    assert agent.pending_turn is None
    assert agent._iteration == 1  # decremented once, not twice


@pytest.mark.asyncio
async def test_discard_pending_turn_rolls_back_when_partial_build_failed():
    """If _set_pending swallowed an exception and left pending_turn=None, discard
    must still roll back the iteration counter.

    Regression: previously the rollback gated on (pending_turn is not None), so
    a swallowed partial-build exception caused _iteration to drift permanently
    higher on every double-failure.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    # Simulate communicate() incrementing the counter and then crashing before
    # _set_pending could finish (partial-build exception swallowed → pending_turn None).
    agent._iteration = 5
    agent._iteration_was_incremented = True
    agent.pending_turn = None

    await agent.discard_pending_turn()
    assert agent._iteration == 4, "rollback must fire even when pending_turn is None"
    assert agent._iteration_was_incremented is False

    # Second call is idempotent — neither signal fires.
    await agent.discard_pending_turn()
    assert agent._iteration == 4


@pytest.mark.asyncio
async def test_stop_clears_pending_turn():
    """stop() clears pending_turn so stale partials don't leak between runs."""
    import tempfile

    from coder_eval.models import TurnRecord

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)
        partial = TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True)
        agent.pending_turn = partial

        await agent.stop()

    assert agent.pending_turn is None


@pytest.mark.asyncio
async def test_claude_agent_start():
    """Test that Claude agent can be started."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write"],
    )

    agent = ClaudeCodeAgent(config)

    # Create a temporary working directory
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        assert agent.working_directory == Path(tmpdir)
        # Client is created per-communicate call, not stored
        assert agent.get_state() == AgentState.WORKING

        # Clean up
        await agent.stop()


def test_claude_agent_disallowed_tools_passed_to_sdk_options():
    """Test that disallowed_tools from AgentConfig reaches ClaudeAgentOptions."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Bash"],
        disallowed_tools=["TodoWrite", "Agent"],
    )

    agent = ClaudeCodeAgent(config)
    assert agent.config.disallowed_tools == ["TodoWrite", "Agent"]


def test_claude_agent_disallowed_tools_defaults_to_none():
    """Test that disallowed_tools defaults to None when not specified."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)
    assert agent.config.disallowed_tools is None


async def _capture_sdk_options(
    agent: ClaudeCodeAgent,
    *,
    env_path_prepend: list[str] | None = None,
    max_turns: int | None = None,
) -> "list":
    """Run one communicate() turn with a mocked query() and return captured options list."""
    import tempfile

    captured_options: list = []

    class ResultMessage:
        def __init__(self, session_id: str = "s-1") -> None:
            self.session_id = session_id
            self.usage = {"input_tokens": 1, "output_tokens": 1}
            self.total_cost_usd = 0.0
            self.num_turns = 1
            self.is_error = False
            self.result = "Done"

    class AssistantMessage:
        def __init__(self) -> None:
            self.content = "ok"
            self.model = "mock-model"

    async def mock_query(prompt, options):
        captured_options.append(options)
        yield AssistantMessage()
        yield ResultMessage()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir, env_path_prepend=env_path_prepend)
        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("hello", max_turns=max_turns)

    return captured_options


@pytest.mark.asyncio
async def test_claude_agent_max_turns_kwarg_reaches_sdk_options():
    """`communicate(max_turns=N)` propagates N to ClaudeAgentOptions.max_turns.

    Regression-guard for the Phase-1 refactor: max_turns is a per-call argument
    (mirrors `timeout`), not a stored field on the agent.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent, max_turns=42)
    assert captured_options[0].max_turns == 42


@pytest.mark.asyncio
async def test_claude_agent_max_turns_default_is_none():
    """Without an explicit max_turns kwarg, ClaudeAgentOptions.max_turns is None (SDK default)."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)
    assert captured_options[0].max_turns is None


@pytest.mark.asyncio
async def test_claude_agent_tool_search_always_disallowed_when_config_empty():
    """ToolSearch is always injected into disallowed_tools even when config specifies none."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["ToolSearch"]
    # Config itself must not be mutated.
    assert agent.config.disallowed_tools is None


@pytest.mark.asyncio
async def test_claude_agent_tool_search_appended_to_user_disallowed_tools():
    """User-specified disallowed_tools are preserved and ToolSearch is appended."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        disallowed_tools=["TodoWrite", "Agent"],
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["TodoWrite", "Agent", "ToolSearch"]
    # Config itself must not be mutated.
    assert agent.config.disallowed_tools == ["TodoWrite", "Agent"]


@pytest.mark.asyncio
async def test_claude_agent_tool_search_not_duplicated():
    """If user already lists ToolSearch, it is not duplicated."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        disallowed_tools=["ToolSearch", "Agent"],
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["ToolSearch", "Agent"]


@pytest.mark.asyncio
async def test_claude_agent_cwd_uses_posix_form():
    """ClaudeAgentOptions.cwd uses forward slashes so bash redirects on Windows don't lose backslashes.

    Anchors the `as_posix()` choice in claude_code_agent.py: bash subprocesses on Windows strip
    backslashes from unquoted paths (e.g. `> D:\\foo\\bar` writes to "Dfoobar"), so the SDK must
    receive a POSIX-style path. On Linux the value is unchanged, so the assertion holds on both
    platforms.
    """
    from pathlib import Path

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    cwd = captured_options[0].cwd
    assert isinstance(cwd, str)
    assert "\\" not in cwd, f"cwd must use POSIX separators, got: {cwd!r}"
    # Cross-check: a Path roundtrip on the captured cwd matches the agent's working dir.
    assert Path(cwd) == agent.working_directory


@pytest.mark.asyncio
async def test_claude_agent_env_path_prepend_propagates_to_sdk_options(monkeypatch):
    """start(env_path_prepend=[...]) -> ClaudeAgentOptions.env['PATH'] is prefixed in order.

    End-to-end check that the orchestrator->agent wiring works: directories passed at
    start() time appear (in order, with the parent PATH appended) on the SDK env so the
    subprocess can shadow real CLIs with sandbox mocks.
    """
    import os

    monkeypatch.setenv("PATH", "/parent/bin")
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent, env_path_prepend=["/sandbox/mocks", "/sandbox/bins"])

    sdk_path = captured_options[0].env["PATH"]
    assert sdk_path == f"/sandbox/mocks{os.pathsep}/sandbox/bins{os.pathsep}/parent/bin"


@pytest.mark.asyncio
async def test_claude_agent_no_env_path_prepend_is_default(monkeypatch):
    """Omitting env_path_prepend at start() leaves PATH equal to the parent PATH."""
    monkeypatch.setenv("PATH", "/parent/bin")
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].env["PATH"] == "/parent/bin"


@pytest.mark.asyncio
async def test_claude_settings_dict_serialized_to_json():
    """Dict claude_settings is JSON-serialized before passing to ClaudeAgentOptions.settings."""
    import json

    settings = {"permissions": {"deny": ["Read(/some/path/**)"]}}
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, claude_settings=settings)
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].settings == json.dumps(settings)


@pytest.mark.asyncio
async def test_claude_settings_string_passthrough():
    """String claude_settings is passed through unchanged (treated as a file path by the SDK)."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, claude_settings="/path/to/settings.json")
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].settings == "/path/to/settings.json"


@pytest.mark.asyncio
async def test_claude_settings_none_default():
    """When claude_settings is None (default), ClaudeAgentOptions.settings is None."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE)
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].settings is None


@pytest.mark.asyncio
async def test_sdk_options_forwarded_to_sdk():
    """An sdk_options key (e.g. effort) is splatted into ClaudeAgentOptions."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, sdk_options={"effort": "medium"})
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].effort == "medium"


@pytest.mark.asyncio
async def test_sdk_options_empty_dict_default():
    """When sdk_options is unset (default {}), the SDK gets no extra kwargs."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE)
    agent = ClaudeCodeAgent(config)
    assert config.sdk_options == {}

    captured_options = await _capture_sdk_options(agent)

    # effort defaults on the SDK side when not passed.
    assert captured_options[0].effort is None


def test_sdk_options_unknown_key_rejected():
    """A key not present on ClaudeAgentOptions is rejected at validation time."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not a ClaudeAgentOptions field"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, sdk_options={"not_a_real_field": 1})


def test_sdk_options_framework_managed_key_rejected():
    """A framework-managed key (e.g. model) cannot be set via sdk_options."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="framework-managed"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, sdk_options={"model": "opus"})


@pytest.mark.parametrize(
    "key",
    [
        "hooks",
        "mcp_servers",
        "cli_path",
        "extra_args",
        "agents",
        "can_use_tool",
        "permission_prompt_tool_name",
        "tools",
        "sandbox",
        "skills",
        "add_dirs",
    ],
)
def test_sdk_options_security_critical_key_rejected(key):
    """Security-critical SDK fields are framework-managed.

    Allowing these via sdk_options would re-open the security holes that
    agent_judge specifically closes with setting_sources=[] — see
    `criteria/agent_judge.py` SECURITY comment.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="framework-managed"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, sdk_options={key: "x"})


def test_sdk_options_validates_on_assignment():
    """validate_assignment re-runs the field validator on attribute writes."""
    from pydantic import ValidationError

    config = AgentConfig(type=AgentKind.CLAUDE_CODE)
    with pytest.raises(ValidationError, match="not a ClaudeAgentOptions field"):
        config.sdk_options = {"bogus": 1}


def test_sdk_options_pydantic_round_trip():
    """AgentConfig with sdk_options survives model_dump -> model_validate cleanly.

    Locks in persistence integrity for EvaluationResult-style serialization
    paths that depend on Pydantic round-trip identity.
    """
    original = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        sdk_options={"effort": "medium", "include_partial_messages": True},
    )
    dumped = original.model_dump()
    restored = AgentConfig.model_validate(dumped)
    assert restored.sdk_options == {"effort": "medium", "include_partial_messages": True}
    assert restored == original


def test_claude_agent_file_change_detection():
    """Test file change detection logic."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # Test detecting created files
    before = {}
    after = {"test.py": 1234567890.0}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "created"

    # Test detecting modified files
    before = {"test.py": 1234567890.0}
    after = {"test.py": 1234567891.0}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "modified"

    # Test detecting deleted files
    before = {"test.py": 1234567890.0}
    after = {}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "deleted"


def test_claude_agent_message_formatting():
    """Test message formatting logic with real SDK message objects.

    Uses the actual claude-agent-sdk classes (not local mock classes with
    the same name) — the formatter now identifies messages via
    ``isinstance``, so mock classes that don't inherit from the SDK types
    would fall through to the unknown-type branch.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from claude_agent_sdk.types import TextBlock

    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # AssistantMessage with a single TextBlock — the canonical SDK shape.
    messages = [AssistantMessage(content=[TextBlock(text="Hello, world!")], model="claude")]
    formatted = agent._format_messages(messages)
    assert "[ASSISTANT] Hello, world!" in formatted

    # ResultMessage success.
    messages = [
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result="File written successfully",
        )
    ]
    formatted = agent._format_messages(messages)
    assert "[RESULT - SUCCESS] File written successfully" in formatted

    # ResultMessage error.
    messages = [
        ResultMessage(
            subtype="error",
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result="File not found",
        )
    ]
    formatted = agent._format_messages(messages)
    assert "[RESULT - ERROR] File not found" in formatted


def test_claude_agent_should_ignore_path():
    """Test path ignoring logic."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # Should ignore these paths
    assert agent._should_ignore_path(Path(".venv/bin/python"))
    assert agent._should_ignore_path(Path("__pycache__/module.pyc"))
    assert agent._should_ignore_path(Path(".git/config"))

    # Should not ignore these paths
    assert not agent._should_ignore_path(Path("src/main.py"))
    assert not agent._should_ignore_path(Path("tests/test_main.py"))


@pytest.mark.asyncio
async def test_claude_agent_lifecycle():
    """Test agent lifecycle (start -> stop)."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Start
        await agent.start(tmpdir)
        # Client is created per-communicate call, not stored during lifecycle
        assert agent.get_state() == AgentState.WORKING

        # Stop
        await agent.stop()
        assert agent.client is None
        assert agent.get_state() == AgentState.FINISHED


def test_claude_agent_message_formatting_edge_cases():
    """Test message formatting with various SDK message types and edge cases.

    Uses real SDK classes throughout — the formatter now identifies messages
    via ``isinstance``, so SDK types and their subclasses (e.g.
    ``TaskStartedMessage`` extends ``SystemMessage``) are handled correctly
    without relying on string-name equality.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        UserMessage,
    )
    from claude_agent_sdk.types import TextBlock

    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    def _make_assistant(text: str) -> AssistantMessage:
        return AssistantMessage(content=[TextBlock(text=text)], model="claude")

    def _make_result(text: str, *, is_error: bool = False) -> ResultMessage:
        return ResultMessage(
            subtype="error" if is_error else "success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=is_error,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result=text,
        )

    # Test 1: Empty messages list
    assert agent._format_messages([]) == "[No output]"

    # Test 2: SystemMessage (filtered out)
    formatted = agent._format_messages([SystemMessage(subtype="init", data={})])
    assert formatted == "[No output]"

    # Test 3: UserMessage (filtered out)
    formatted = agent._format_messages([UserMessage(content="test")])
    assert formatted == "[No output]"

    # Test 4: tool_use stream event — duck-typed; the SDK doesn't export a
    # public StreamEvent class, so the formatter matches on attribute shape.
    class _ToolUseEvent:
        type = "tool_use"
        name = "Read"

    formatted = agent._format_messages([_ToolUseEvent()])
    assert "[TOOL USE] Read" in formatted

    # Test 5: Non-tool_use event of the same shape — falls through to the
    # unknown-tag branch (was previously filtered; now we surface "an
    # unknown message type appeared" via its class name).
    class _ThinkingEvent:
        type = "thinking"

    formatted = agent._format_messages([_ThinkingEvent()])
    assert formatted == "[_ThinkingEvent]"

    # Test 6: Unknown message type — only the type-name tag is emitted.
    # The body (``str(msg)``) is intentionally NOT included; see the long
    # docstring on ``_format_messages`` and
    # ``test_format_messages_unknown_type_does_not_leak_unbalanced_braces``
    # for the rationale.
    class CustomMessage:
        def __str__(self):
            return "custom content here"

    formatted = agent._format_messages([CustomMessage()])
    assert "[CustomMessage]" in formatted
    # Body must NOT leak — protects downstream JSON parsers from
    # truncated ``__repr__`` output with unmatched braces.
    assert "custom content" not in formatted

    # Test 7: Message without expected attributes (defensive getattr)
    class BareMessage:
        pass

    formatted = agent._format_messages([BareMessage()])
    assert "[BareMessage]" in formatted

    # Test 8: Multiple message types in sequence
    messages = [
        SystemMessage(subtype="init", data={}),  # Filtered
        UserMessage(content="user input"),  # Filtered
        _make_assistant("Hello from assistant"),
        _make_result("Operation successful", is_error=False),
        _ToolUseEvent(),
        _ThinkingEvent(),  # Now surfaced as ``[_ThinkingEvent]``
    ]
    formatted = agent._format_messages(messages)

    assert "[ASSISTANT] Hello from assistant" in formatted
    assert "[RESULT - SUCCESS] Operation successful" in formatted
    assert "[TOOL USE] Read" in formatted
    assert "SystemMessage" not in formatted
    assert "user input" not in formatted

    # Test 9: ResultMessage with error
    formatted = agent._format_messages([_make_result("File not found", is_error=True)])
    assert "[RESULT - ERROR] File not found" in formatted

    # Test 10: AssistantMessage with empty content
    formatted = agent._format_messages([AssistantMessage(content=[], model="claude")])
    assert formatted == "[No output]"

    # Test 11: Multiple AssistantMessages
    messages = [_make_assistant("First response"), _make_assistant("Second response")]
    formatted = agent._format_messages(messages)
    assert "[ASSISTANT] First response" in formatted
    assert "[ASSISTANT] Second response" in formatted
    assert formatted.count("[ASSISTANT]") == 2


def test_format_messages_system_message_subclasses_are_filtered():
    """Regression: SystemMessage SUBCLASSES (TaskStartedMessage, etc.) must
    be filtered out the same way SystemMessage itself is.

    claude-agent-sdk 0.1.x added ``TaskStartedMessage``,
    ``TaskNotificationMessage``, and ``TaskProgressMessage`` for sub-agent
    lifecycle reporting. Each is declared as a subclass of
    ``SystemMessage`` with an explicit drop-in contract:

        "Subclass of SystemMessage: existing ``isinstance(msg,
        SystemMessage)`` and ``case SystemMessage()`` checks continue to
        match."

    An earlier version of ``_format_messages`` compared the exact
    ``type(msg).__name__`` string against ``"SystemMessage"``, which
    defeated the SDK's drop-in design — the subclasses fell through to
    an "unknown message type" branch that ran ``str(msg)[:100]`` and
    emitted a truncated Python-repr containing nested ``data={...}``
    dict literals. Even though the typed verdict tool channel has
    since obviated the brace-walking verdict parser that originally
    motivated this fix, the underlying ``isinstance``-vs-name-equality
    contract is still worth pinning.

    This test exercises the real SDK ``TaskStartedMessage`` instance
    (not a name-collision mock) and asserts:

      1. The lifecycle message is silently filtered (not emitted as a
         tag, exactly as ``SystemMessage`` itself would be).
      2. A verdict-shaped JSON literal in a sibling ``AssistantMessage``
         survives intact in the formatter output.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TaskNotificationMessage,
        TaskStartedMessage,
    )
    from claude_agent_sdk.types import TextBlock

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    # Sanity-check: the SDK classes still extend SystemMessage as the
    # docstring promises. If this regresses upstream, our isinstance
    # filter would silently revert to the buggy unknown-type path.
    assert issubclass(TaskStartedMessage, SystemMessage)
    assert issubclass(TaskNotificationMessage, SystemMessage)

    verdict_json = '{"score": 1.0, "rationale": "all good"}'

    messages = [
        TaskStartedMessage(
            subtype="task_started",
            data={
                "type": "system",
                "subtype": "task_started",
                "task_id": "abc-def-long-id",
                "message": "starting sub-task",
            },
            task_id="abc-def-long-id",
            description="starting sub-task",
            uuid="u",
            session_id="s",
            tool_use_id=None,
            task_type="general",
        ),
        TaskNotificationMessage(
            subtype="task_notification",
            data={"type": "system", "subtype": "task_notification"},
            task_id="abc-def-long-id",
            status="completed",
            output_file=None,
            summary=None,
            uuid="u",
            session_id="s",
            tool_use_id=None,
            usage=None,
        ),
        AssistantMessage(content=[TextBlock(text=verdict_json)], model="claude"),
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result=verdict_json,
        ),
    ]
    formatted = agent._format_messages(messages)

    # SystemMessage subclasses are silently filtered (no tag emitted).
    assert "TaskStartedMessage" not in formatted
    assert "TaskNotificationMessage" not in formatted

    # The verdict JSON survives, brace counts are balanced.
    assert formatted.count("{") == formatted.count("}")

    # Formatter contract: verdict JSON survives intact in the textual transcript
    # used for log auditing. The judge no longer parses this output — it's
    # purely a human-readable artifact now — but a regression that drops or
    # truncates the verdict text would still mask debugging signal.
    assert verdict_json in formatted


@pytest.mark.asyncio
async def test_claude_agent_process_error_includes_stderr():
    """Test that ProcessError is caught and its stderr is included in RuntimeError."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        # query() returns an async generator, so mock must be one too
        async def mock_query(*args, **kwargs):
            raise ProcessError("process failed", exit_code=1, stderr="Error: invalid config")
            yield  # makes this an async generator

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(RuntimeError, match=r"CLI process failed \(exit code 1\): Error: invalid config"),
        ):
            await agent.communicate("do something")

        assert agent.get_state() == AgentState.ERROR


@pytest.mark.asyncio
async def test_claude_agent_process_error_no_stderr_at_all():
    """Test that ProcessError with no stderr and no stderr_lines shows sentinel message."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(*args, **kwargs):
            raise ProcessError("process failed", exit_code=None, stderr=None)
            yield  # makes this an async generator

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(RuntimeError, match=r"CLI process failed \(exit code None\): No stderr captured"),
        ):
            await agent.communicate("do something")


@pytest.mark.asyncio
async def test_claude_agent_session_resumption():
    """Test that session_id from first communicate() is passed as resume on subsequent calls."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        # Track options passed to query() across calls
        captured_options = []

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield AssistantMessage()
            yield ResultMessage(session_id="test-session-abc")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            # First call: no session_id yet
            await agent.communicate("first prompt")
            assert captured_options[0].resume is None
            assert agent._session_id == "test-session-abc"

            # Second call: should pass session_id as resume
            await agent.communicate("second prompt")
            assert captured_options[1].resume == "test-session-abc"


@pytest.mark.asyncio
async def test_claude_agent_errored_result_does_not_commit_session_id():
    """When a ResultMessage arrives with is_error=True, the agent must NOT
    commit the attached session_id. Otherwise a retry (e.g. after the CLI
    crashes mid-turn) resumes from a broken transcript and often
    reproduces the same crash — exactly the failure mode that made UIA
    smoke runs permanently red."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        class ResultMessage:
            def __init__(self, session_id: str, is_error: bool):
                self.session_id = session_id
                self.usage = {"input_tokens": 1, "output_tokens": 1}
                self.total_cost_usd = 0.0
                self.num_turns = 1
                self.is_error = is_error
                self.result = None

        class AssistantMessage:
            def __init__(self) -> None:
                self.content = "..."
                self.model = "mock-model"

        # First: a clean turn commits session_id as usual.
        async def mock_ok(prompt, options):
            yield AssistantMessage()
            yield ResultMessage(session_id="good-session", is_error=False)

        with patch("coder_eval.agents.claude_code_agent.query", mock_ok):
            await agent.communicate("clean turn")
            assert agent._session_id == "good-session"

        # Second: an errored turn arriving with a NEW session_id must NOT
        # overwrite the previous good one, so the next retry resumes
        # from the last-known-good state (or restarts clean if no prior
        # good turn existed).
        async def mock_err(prompt, options):
            yield AssistantMessage()
            yield ResultMessage(session_id="poisoned-session", is_error=True)

        with patch("coder_eval.agents.claude_code_agent.query", mock_err):
            await agent.communicate("errored turn")
            assert agent._session_id == "good-session"


@pytest.mark.asyncio
async def test_claude_agent_session_resumption_none_degrades_gracefully():
    """When SDK returns session_id=None, agent should degrade to a fresh session."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        captured_options = []

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield AssistantMessage()
            yield ResultMessage(session_id=None)

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("first prompt")
            assert agent._session_id is None

            # Second call: resume should be None (fresh session)
            await agent.communicate("second prompt")
            assert captured_options[1].resume is None


@pytest.mark.asyncio
async def test_claude_agent_session_rotation():
    """When SDK returns a different session_id on second call, agent should use the new one."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        captured_options = []
        call_count = 0

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            nonlocal call_count
            captured_options.append(options)
            yield AssistantMessage()
            # Return different session_id on each call
            call_count += 1
            yield ResultMessage(session_id=f"session-{call_count}")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("first prompt")
            assert agent._session_id == "session-1"

            await agent.communicate("second prompt")
            assert captured_options[1].resume == "session-1"
            assert agent._session_id == "session-2"

            # Third call should use the rotated session_id
            await agent.communicate("third prompt")
            assert captured_options[2].resume == "session-2"


@pytest.mark.asyncio
async def test_claude_agent_session_retained_on_error():
    """On error, _session_id should retain its value from the last successful result."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        # First call succeeds and sets session_id
        async def mock_query_ok(prompt, options):
            yield AssistantMessage()
            yield ResultMessage(session_id="good-session")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query_ok):
            await agent.communicate("first prompt")
            assert agent._session_id == "good-session"

        # Second call raises an error mid-stream
        async def mock_query_error(prompt, options):
            raise RuntimeError("SDK connection lost")
            yield

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query_error),
            pytest.raises(RuntimeError, match="SDK connection lost"),
        ):
            await agent.communicate("second prompt")

        # session_id should still be the value from the successful call
        assert agent._session_id == "good-session"


class TestResultSummaryAndFormatter:
    """The agent persists the SDK's final ResultMessage as a structured
    ``ResultSummary`` on every TurnRecord (success and error), and the error
    paths read from that single source instead of reverse-scanning messages.
    These tests anchor the helpers against the real SDK dataclass — so a
    rename of subtype / stop_reason / result trips them immediately."""

    @staticmethod
    def _make_result(**overrides):
        from claude_agent_sdk.types import ResultMessage

        defaults = {
            "subtype": "success",
            "duration_ms": 0,
            "duration_api_ms": 0,
            "is_error": False,
            "num_turns": 1,
            "session_id": "s1",
            "stop_reason": None,
            "total_cost_usd": None,
            "usage": {},
            "result": None,
            "structured_output": None,
        }
        defaults.update(overrides)
        return ResultMessage(**defaults)

    def test_summarize_returns_none_for_non_result_message(self):
        class NotAResult:
            pass

        assert ClaudeCodeAgent._summarize_result(NotAResult()) is None  # type: ignore[arg-type]

    def test_summarize_defaults_subtype_to_unknown_when_missing(self):
        """Test stand-ins (with session_id + usage but no subtype) must still
        produce a usable summary so consumers like the session-id retention
        branch can read summary.is_error uniformly."""
        from types import SimpleNamespace

        msg = SimpleNamespace(session_id="s1", usage={}, is_error=True, stop_reason=None, result=None)
        summary = ClaudeCodeAgent._summarize_result(msg)  # type: ignore[arg-type]
        assert summary is not None
        assert summary.subtype == "unknown"
        assert summary.is_error is True

    def test_summarize_captures_diagnostic_fields(self):
        msg = self._make_result(
            is_error=True,
            subtype="error_during_execution",
            stop_reason="tool_error",
            result="boom",
        )
        summary = ClaudeCodeAgent._summarize_result(msg)
        assert summary is not None
        assert summary.is_error is True
        assert summary.subtype == "error_during_execution"
        assert summary.stop_reason == "tool_error"
        assert summary.result == "boom"

    def test_format_returns_none_when_summary_is_none(self):
        assert ClaudeCodeAgent._format_error_summary(None) is None

    def test_format_returns_none_when_not_an_error(self):
        summary = ClaudeCodeAgent._summarize_result(self._make_result(is_error=False, result="ignored"))
        assert ClaudeCodeAgent._format_error_summary(summary) is None

    def test_format_prefers_result_text(self):
        summary = ClaudeCodeAgent._summarize_result(
            self._make_result(is_error=True, result="Something exploded", subtype="error_during_execution")
        )
        assert ClaudeCodeAgent._format_error_summary(summary) == "Something exploded"

    def test_format_falls_back_to_subtype_and_stop_reason(self):
        summary = ClaudeCodeAgent._summarize_result(
            self._make_result(is_error=True, subtype="error_during_execution", stop_reason="tool_error")
        )
        assert ClaudeCodeAgent._format_error_summary(summary) == (
            "Result[is_error=True]: error_during_execution / tool_error"
        )

    def test_format_omits_stop_reason_when_unset(self):
        summary = ClaudeCodeAgent._summarize_result(self._make_result(is_error=True, subtype="error_during_execution"))
        assert ClaudeCodeAgent._format_error_summary(summary) == "Result[is_error=True]: error_during_execution"


@pytest.mark.asyncio
async def test_communicate_persists_result_summary_on_turn_record():
    """End-to-end: a successful turn carries a ResultSummary on the TurnRecord,
    so downstream analysis (dashboards, post-mortem) can read SDK status without
    re-walking the message stream."""
    from claude_agent_sdk.types import ResultMessage

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        class AssistantMessage:
            content = "ok"
            model = "mock-model"

        async def mock_query(prompt, options):
            yield AssistantMessage()
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=4,
                session_id="s1",
                stop_reason="end_turn",
                total_cost_usd=0.0,
                usage={"input_tokens": 1, "output_tokens": 1},
                result="all good",
                structured_output=None,
            )

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            turn = await agent.communicate("hello")

        assert turn.result_summary is not None
        assert turn.result_summary.is_error is False
        assert turn.result_summary.subtype == "success"
        assert turn.result_summary.stop_reason == "end_turn"
        assert turn.result_summary.result == "all good"
        # SDK-reported num_turns propagated to TurnRecord.num_turns
        assert turn.num_turns == 4


@pytest.mark.asyncio
async def test_claude_agent_crash_preserves_partial_turn_record():
    """When communicate() fails mid-turn, agent.pending_turn carries a partial
    TurnRecord populated with tool calls captured before the crash.

    This is the whole point of the pending_turn slot + on_attempt_error
    plumbing: typed criteria like skill_triggered must still be able to
    observe a Skill invocation that happened before the crash.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    class AssistantMessage:
        def __init__(self, blocks):
            self.content = blocks
            self.model = "mock-model"

    class ToolUseBlock:
        def __init__(self, name, tool_id, input_):
            self.name = name
            self.id = tool_id
            self.input = input_

    async def mock_query(prompt, options, transport=None):
        # Agent invokes a Skill, then the stream dies before completing.
        yield AssistantMessage([ToolUseBlock("Skill", "tool-1", {"skill": "my_skill"})])
        raise RuntimeError("subprocess exited unexpectedly")

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(AgentCrashError),
        ):
            await agent.communicate("do the thing")

        # Slot is populated before the raise; not yet cleared (caller must drain).
        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        assert partial.max_turns_exhausted is False
        # No ResultMessage arrived before the crash, so num_turns is None.
        assert partial.num_turns is None
        # The Skill invocation that happened before the crash is preserved.
        assert len(partial.commands) == 1
        assert partial.commands[0].tool_name == "Skill"
        assert partial.commands[0].parameters == {"skill": "my_skill"}
        # Iteration contract: partial carries the bumped iteration number; the
        # counter is NOT rolled back until discard_pending_turn() is called.
        assert partial.iteration == 1
        assert agent._iteration == 1

        await agent.discard_pending_turn()
        assert agent.pending_turn is None
        assert agent._iteration == 0


@pytest.mark.asyncio
async def test_claude_agent_crash_partial_carries_crash_reason():
    """The agent must stamp ``crash_reason`` on the partial at construction.

    Stamping at the raise site means the model is finalised before any
    downstream consumer touches it — keeps the partial-preservation flow
    safe against a future ``frozen=True`` on TurnRecord, and removes the
    orchestrator's post-construction mutation as the primary stamping point.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    async def mock_query(prompt, options, transport=None):
        raise RuntimeError("CLI process failed (exit code 137): killed")
        yield  # pragma: no cover

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(AgentCrashError),
        ):
            await agent.communicate("go")

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crash_reason is not None
        # The crash message is truncated at 200 chars, but a short message
        # passes through verbatim.
        assert "CLI process failed (exit code 137)" in partial.crash_reason


@pytest.mark.asyncio
async def test_claude_agent_timeout_partial_carries_crash_reason():
    """TurnTimeoutError partials must carry the normalised "timed out after Ns"
    reason at construction (not via post-hoc mutation)."""
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    async def mock_query(prompt, options, transport=None):
        raise RuntimeError("watchdog kill")
        yield  # pragma: no cover

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            patch.object(ClaudeCodeAgent, "_timed_out", staticmethod(lambda *a, **k: True)),
            pytest.raises(TurnTimeoutError),
        ):
            await agent.communicate("go", timeout=42.0)

        partial = agent.pending_turn
        assert partial is not None
        # Normalised reason: integer-second formatting matches the
        # orchestrator's defensive fallback so report rendering is consistent.
        assert partial.crash_reason == "Agent turn timed out after 42s"


@pytest.mark.asyncio
async def test_claude_agent_repeated_crashes_keep_iteration_stable():
    """Consecutive crashes in one orchestrator iteration all carry the same iteration number.

    discard_pending_turn() rolls back _iteration after each crash (simulating
    what the orchestrator does), so repeated failures in a single logical
    orchestrator iteration all stamp the same iteration on their partial records.
    A subsequent clean call then advances the counter by one. This is what the
    orchestrator's multiple-partials-per-iteration contract relies on.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    class AssistantMessage:
        def __init__(self, blocks):
            self.content = blocks
            self.model = "mock-model"

    class ToolUseBlock:
        def __init__(self, name, tool_id, input_):
            self.name = name
            self.id = tool_id
            self.input = input_

    async def crashing_query(prompt, options, transport=None):
        yield AssistantMessage([ToolUseBlock("Read", "tool-crash", {"file": "x"})])
        raise RuntimeError("stream died")

    clean_finished = False

    async def clean_query(prompt, options, transport=None):
        # Match the SDK shape minimally: yield one AssistantMessage then end.
        nonlocal clean_finished
        yield AssistantMessage([ToolUseBlock("Read", "tool-clean", {"file": "y"})])
        clean_finished = True

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        partials: list = []
        for _ in range(3):
            with (
                patch("coder_eval.agents.claude_code_agent.query", crashing_query),
                pytest.raises(AgentCrashError),
            ):
                await agent.communicate("go")
            partials.append(agent.pending_turn)
            # Simulate the orchestrator draining and discarding after a failed attempt.
            await agent.discard_pending_turn()
            assert agent._iteration == 0

        assert all(p is not None and p.iteration == 1 and p.crashed for p in partials)

        # The clean retry advances the counter and produces iteration=1 again,
        # so all four records for this logical orchestrator iteration share 1.
        with patch("coder_eval.agents.claude_code_agent.query", clean_query):
            turn_record = await agent.communicate("go")

        assert clean_finished
        assert turn_record.iteration == 1
        assert turn_record.crashed is False
        assert agent._iteration == 1


@pytest.mark.asyncio
async def test_claude_agent_timeout_preserves_partial_turn_record():
    """agent.pending_turn carries a partial TurnRecord with pre-kill tool calls
    after a TurnTimeoutError.

    Watchdog-killed turns are exactly where observational telemetry is
    most valuable (an agent that looped on tool calls and ran the wall
    clock out). Mirrors the AgentCrashError partial-preservation path.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    class AssistantMessage:
        def __init__(self, blocks):
            self.content = blocks
            self.model = "mock-model"

    class ToolUseBlock:
        def __init__(self, name, tool_id, input_):
            self.name = name
            self.id = tool_id
            self.input = input_

    async def mock_query(prompt, options, transport=None):
        # Agent invokes one tool, then the watchdog kills the subprocess,
        # which the SDK surfaces as a generic Exception.
        yield AssistantMessage([ToolUseBlock("Bash", "tool-t", {"command": "sleep 1000"})])
        raise RuntimeError("process terminated by watchdog")

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        # Force the agent's timeout-detection helper to return True so the
        # raised RuntimeError is classified as a timeout. Avoids needing
        # real wall-clock waits in the test.
        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            patch.object(ClaudeCodeAgent, "_timed_out", staticmethod(lambda *a, **k: True)),
            pytest.raises(TurnTimeoutError),
        ):
            await agent.communicate("start", timeout=0.01)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        assert len(partial.commands) == 1
        assert partial.commands[0].tool_name == "Bash"
        # Slot carries the bumped iteration; counter rolls back after discard.
        assert partial.iteration == 1
        assert agent._iteration == 1
        await agent.discard_pending_turn()
        assert agent._iteration == 0


@pytest.mark.asyncio
async def test_claude_agent_error_max_turns_is_clean_completion_not_crash():
    """SDK ``error_max_turns`` (subtype + is_error + exit 1) must not raise AgentCrashError.

    The CLI emits a ResultMessage with ``subtype="error_max_turns"`` and
    ``is_error=True``, then exits 1 — which the SDK re-raises as
    ``ProcessError``. That is a legitimate "agent ran out of turns"
    outcome, not a crash. Treating it as AGENT_CRASH would make it
    retryable (max_retries=2) and resume the same prompt that just
    burned its turn budget — pure waste. Instead the agent falls
    through to the success path so the orchestrator's existing
    ``max_turns_exhausted`` handling can stop iterating.
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    class AssistantMessage:
        def __init__(self, blocks):
            self.content = blocks
            self.model = "mock-model"

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ResultMessage:
        # Mirror the SDK shape that the agent's _is_sdk_result_message + _summarize_result expect.
        def __init__(self):
            self.session_id = "s-1"
            self.usage = {"input_tokens": 100, "output_tokens": 50}
            self.total_cost_usd = 0.01
            self.num_turns = 11  # > max_turns=10
            self.is_error = True
            self.subtype = "error_max_turns"
            self.stop_reason = "tool_use"
            self.result = None

    async def mock_query(prompt, options, transport=None):
        yield AssistantMessage([TextBlock("working on it")])
        yield ResultMessage()
        raise ProcessError("Command failed with exit code 1", exit_code=1, stderr="")

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            # Must NOT raise: error_max_turns is a clean completion path.
            turn_record = await agent.communicate("solve something hard")

        assert turn_record.crashed is False
        assert turn_record.max_turns_exhausted is True
        # Iteration counter advances normally on a clean turn (no rollback).
        assert agent._iteration == 1
        # The ResultMessage details are still captured for diagnostics.
        assert turn_record.result_summary is not None
        assert turn_record.result_summary.subtype == "error_max_turns"


@pytest.mark.asyncio
async def test_claude_agent_error_max_turns_clean_completion_via_exception_path():
    """Sibling of the ProcessError test: the SDK sometimes wraps the
    underlying failure as a generic ``Exception`` rather than re-raising
    ``ProcessError`` directly. ``_max_turns_short_circuit`` lives in both
    ``except`` branches; this asserts the ``except Exception`` branch
    short-circuits the same way (clean completion, no crash, no rollback).
    """
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    class AssistantMessage:
        def __init__(self, blocks):
            self.content = blocks
            self.model = "mock-model"

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ResultMessage:
        def __init__(self):
            self.session_id = "s-1"
            self.usage = {"input_tokens": 100, "output_tokens": 50}
            self.total_cost_usd = 0.01
            self.num_turns = 11
            self.is_error = True
            self.subtype = "error_max_turns"
            self.stop_reason = "tool_use"
            self.result = None

    async def mock_query(prompt, options, transport=None):
        yield AssistantMessage([TextBlock("working on it")])
        yield ResultMessage()
        # The SDK may wrap the underlying CLI failure as a bare Exception
        # rather than ProcessError — exercise the except-Exception branch.
        raise RuntimeError("SDK stream wrapped the CLI exit-1 as a bare Exception")

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            turn_record = await agent.communicate("solve something hard")

        assert turn_record.crashed is False
        assert turn_record.max_turns_exhausted is True
        assert agent._iteration == 1
        assert turn_record.result_summary is not None
        assert turn_record.result_summary.subtype == "error_max_turns"
