"""Tests for the Antigravity agent backend (registration, config, token/tool mapping).

These exercise the wiring and the pure mapping helpers; they do NOT require the
optional ``google-antigravity`` SDK (all SDK use is lazy, inside ``start()``).
"""

import asyncio
import inspect
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from coder_eval.agents import antigravity_agent as agent_module
from coder_eval.agents.antigravity_agent import (
    _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP,
    _DEFAULT_MODEL,
    AntigravityAgent,
    _AntigravityDecoder,
    _enum_value,
    _to_token_usage,
)
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import (
    AgentKind,
    AgentState,
    AntigravityAgentConfig,
    AssistantMessage,
    RunLimits,
    TimingBasis,
    parse_agent_config,
)
from coder_eval.orchestration.plugin_staging import stage_plugins
from coder_eval.orchestration.turn_monitor import TurnMonitor
from coder_eval.plugins import ensure_plugins_loaded
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.emitter import TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StopReason,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
)
from coder_eval.testing import (
    Replay,
    ScriptedClock,
    Tick,
    assert_identity_closes,
    assert_stream_balanced,
    replay,
)
from tests._bracket_clock import AnchoredClock, assert_bracket_on_the_clock, assert_overhead_is_measured
from tests._fixtures.golden_streams._scrub import assert_reconciliation
from tests._fixtures.golden_streams.antigravity_fixtures import (
    _agent_with_steps,
    _FakeConversation,
    _no_sleep,
    _step,
    _tc,
    _usage,
)


def test_antigravity_registered_to_agent_and_config():
    """The built-in plugin hook registers antigravity → AntigravityAgent/Config."""
    ensure_plugins_loaded()
    reg = AgentRegistry.get(AgentKind.ANTIGRAVITY)
    assert reg is not None
    assert reg.agent_class is AntigravityAgent
    assert reg.config_class is AntigravityAgentConfig


def test_config_dispatch_and_thinking_default():
    """parse_agent_config routes type=antigravity to its config; thinking defaults to medium."""
    cfg = parse_agent_config(type="antigravity")
    assert isinstance(cfg, AntigravityAgentConfig)
    assert cfg.thinking_level == "medium"
    assert cfg.model is None


@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high"])
def test_config_accepts_valid_thinking_levels(level: str):
    cfg = parse_agent_config(type="antigravity", thinking_level=level)
    assert isinstance(cfg, AntigravityAgentConfig)
    assert cfg.thinking_level == level


def test_config_rejects_invalid_thinking_level():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_agent_config(type="antigravity", thinking_level="ultra")


def test_effective_model_prefers_config_then_default():
    """agent.model wins; otherwise the recommended Gemini 3 Pro default applies."""
    pinned = AntigravityAgent(parse_agent_config(type="antigravity", model="gemini-3.5-flash"))
    assert pinned._effective_model() == "gemini-3.5-flash"

    unpinned = AntigravityAgent(parse_agent_config(type="antigravity"))
    # No ANTIGRAVITY_MODEL set in the test env → falls through to the default.
    assert unpinned._effective_model() == _DEFAULT_MODEL


def test_environment_info_reports_append_prompt_semantics():
    """Antigravity always appends system_prompt (TemplatedSystemInstructions);
    the cross-agent marker in run.json records that regime."""
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))

    assert agent.get_environment_info()["system_prompt_semantics"] == "append"


def _staged_root(tmp_path: Path) -> Path:
    """A plugin root staged by ``stage_plugins`` over one authored skill."""
    skill = tmp_path / "authored" / "skills" / "uipath-sdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# uipath-sdd\n")
    return stage_plugins([{"type": "local", "path": str(tmp_path / "authored")}], tmp_path / "plugin_root").root


@pytest.mark.parametrize("staged", [True, False])
async def test_start_delivers_the_staged_skills_dir(tmp_path, monkeypatch, staged):
    """``skills_paths`` is exactly ``[<root>/skills]`` and that entry joins ``workspaces``."""
    root = _staged_root(tmp_path) if staged else None
    configs: list[Any] = []

    class _RecordingSdkAgent:
        def __init__(self, cfg: Any) -> None:
            configs.append(cfg)

        async def __aenter__(self) -> "_RecordingSdkAgent":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    _install_fake_sdk(monkeypatch, _RecordingSdkAgent)
    work = tmp_path / "work"
    await AntigravityAgent(parse_agent_config(type="antigravity")).start(str(work), plugin_root=root)

    expected = [str(root / "skills")] if root is not None else []
    assert configs[0].skills_paths == expected
    sources = [str((tmp_path / "authored" / "skills" / "uipath-sdd").resolve())] if root is not None else []
    assert configs[0].workspaces == [str(work), *expected, *sources]


def test_workspace_only_permits_reading_a_staged_skill(tmp_path):
    """Drives the harness's real ``workspace_only`` policy over a staged root: reading
    ``<root>/skills/<name>/SKILL.md`` must be permitted by the delivered ``workspaces``."""
    policy = pytest.importorskip("google.antigravity.hooks.policy")
    ag_types = pytest.importorskip("google.antigravity.types")

    root = _staged_root(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = workdir

    policies = policy.workspace_only(agent._resolve_workspaces(root))
    tc = ag_types.ToolCall(name="read_file", canonical_path=str(root / "skills" / "uipath-sdd" / "SKILL.md"))
    assert not any(p.when(tc) for p in policies if p.when is not None)


def test_to_token_usage_maps_gemini_buckets():
    """Gemini UsageMetadata → coder_eval TokenUsage: uncached=prompt-cached,
    cache_read=cached, cache_creation=0, output=candidates+thoughts."""
    usage = SimpleNamespace(
        prompt_token_count=1000,
        cached_content_token_count=200,
        candidates_token_count=50,
        thoughts_token_count=30,
        total_token_count=1080,
    )
    tu = _to_token_usage(usage, model="gemini-3.1-pro-preview")
    assert tu.uncached_input_tokens == 800
    assert tu.cache_read_input_tokens == 200
    assert tu.cache_creation_input_tokens == 0
    assert tu.output_tokens == 80
    # input_tokens is the derived total of the three input buckets.
    assert tu.input_tokens == 1000
    # Cost is rate-carded (Pro: $2/M in, $12/M out, $0.20/M cached).
    expected = (800 * 2.0 + 80 * 12.0 + 200 * 0.20) / 1_000_000
    assert tu.total_cost_usd == pytest.approx(expected)


def test_to_token_usage_handles_missing_fields():
    """A bare usage object (None counters) maps to an all-zero TokenUsage."""
    tu = _to_token_usage(SimpleNamespace(), model=None)
    assert tu.is_empty()
    assert tu.total_cost_usd is None


def test_enum_value_unwraps_str_enum_and_passes_plain():
    import enum

    class S(enum.StrEnum):
        A = "VALUE_A"

    assert _enum_value(S.A) == "VALUE_A"
    assert _enum_value("plain") == "plain"


def test_tool_name_map_covers_core_builtins():
    """The Gemini builtin tools map to the canonical Claude-ish names criteria key on."""
    assert _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP["run_command"] == "Bash"
    assert _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP["create_file"] == "Write"
    assert _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP["edit_file"] == "Edit"
    assert _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP["view_file"] == "Read"


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-customtools",
        "gemini-3-pro-preview",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview",
    ],
)
def test_gemini_models_are_priced(model: str):
    """Every Gemini model the backend may run has a rate-card entry (cost is not None)."""
    cost = calculate_cost(model, 1000, 1000, 0, 0)
    assert cost is not None and cost > 0


# --- communicate() step-stream mapping (SDK mocked via fake Step stream) ---------


_WATCHDOG = "coder_eval.agents.watchdog.ThreadedWatchdog"


class _FiringWatchdog:
    """Fake ThreadedWatchdog that fires ``on_timeout`` synchronously at entry.

    Sets ``decoder.timeout_hit = True`` before any draining happens (exactly
    like the real watchdog thread firing early), and reports ``fired`` so a
    CancelledError the body raises later surfaces from ``run_with_watchdog`` as
    ``WatchdogFired`` — the watchdog's own cancel, not the caller's.
    """

    fired = True

    def __init__(self, *, on_timeout, **_kwargs):
        self._on_timeout = on_timeout

    def __enter__(self):
        self._on_timeout()
        return self

    def __exit__(self, *_exc):
        return False


async def test_communicate_maps_steps_to_turn_record():
    """A realistic think→edit→run→respond stream yields mapped commands, summed
    tokens, and the final assistant text — exercising the full mapping path."""
    steps = [
        _step("THINKING", "DONE", thinking="planning", usage=_usage(1000, 0, 10, 20)),
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("edit_file", "t1", {"file_path": "hello.py"})],
            content="Create hello.py",
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("edit_file", "t1", {"file_path": "hello.py", "diff_block": "+print('hi')"})],
        ),
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t2", {"command_line": "python hello.py"})],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("run_command", "t2", {"command_line": "python hello.py", "exit_code": 0, "combined_output": "hi"})
            ],
            usage=_usage(1200, 0, 15, 5),
        ),
        _step(
            "TEXT_RESPONSE",
            "DONE",
            content="All done.",
            content_delta="All done.",
            complete=True,
            usage=_usage(1300, 0, 30, 0),
        ),
    ]
    agent = _agent_with_steps(steps)
    tr = (await agent.communicate("make hello.py", iteration=1)).record

    assert tr.crashed is False
    assert tr.agent_output == "All done."
    names = [c.tool_name for c in tr.commands]
    assert names == ["Edit", "Bash"]  # mapped + ordered by sequence
    # The run_command result (exit_code 0) is recorded as success.
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "success"
    # Bash params are canonicalized: command_line -> command, and the harness
    # result payload (exit_code / combined_output) is stripped, not leaked.
    assert bash.parameters == {"command": "python hello.py"}
    edit = next(c for c in tr.commands if c.tool_name == "Edit")
    assert "diff_block" not in edit.parameters  # DONE-only result field dropped
    assert edit.parameters.get("file_path") == "hello.py"
    assert tr.token_usage is not None
    # output = Σ(candidates+thoughts) over the three usage-bearing generations.
    assert tr.token_usage.output_tokens == (10 + 20) + (15 + 5) + (30 + 0)
    assert tr.token_usage.uncached_input_tokens == 1000 + 1200 + 1300
    assert tr.assistant_turn_count == 3
    # Reconciliation invariant (CLAUDE.md): summing the four per-message token
    # buckets across TurnRecord.messages (assistant + reconciliation) equals the
    # turn-level token_usage exactly — the source of truth the evalboard SUMs.
    bucketed = [m for m in tr.messages if hasattr(m, "cache_creation_tokens")]
    assert bucketed, "expected at least one bucketed (assistant/reconciliation) message"
    assert sum(m.input_tokens for m in bucketed) == tr.token_usage.uncached_input_tokens
    assert sum(m.output_tokens for m in bucketed) == tr.token_usage.output_tokens
    assert sum(m.cache_creation_tokens for m in bucketed) == tr.token_usage.cache_creation_input_tokens
    assert sum(m.cache_read_tokens for m in bucketed) == tr.token_usage.cache_read_input_tokens
    # Every generation carries its own identity. Filter explicitly: `tr.messages`
    # is list[TranscriptMessage] and ReconciliationMessage has no `message_id`,
    # so a bare comprehension would raise the moment a residual is booked.
    # The literal strings pin the 0-based Codex-parity scheme, which mere
    # distinctness (a uuid would pass) does not.
    ids = [m.message_id for m in tr.messages if isinstance(m, AssistantMessage)]
    assert ids == ["antigravity-1-msg-0", "antigravity-1-msg-1", "antigravity-1-msg-2"]


async def test_communicate_normalizes_arg_keys_and_strips_done_only_results():
    """LS directory_path -> path, and tool-specific result fields that first
    appear at DONE (LS ``results``, WebSearch ``summary``) are stripped from
    parameters — so skill_triggered can't false-positive on a leaked result."""
    steps = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("list_directory", "t1", {"directory_path": "/work"})],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("list_directory", "t1", {"directory_path": "/work", "results": "skills/uipath-agents/SKILL.md"})
            ],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("search_web", "t2", {"query": "uipath", "summary": "skills/uipath-agents/ mention"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    tr = (await _agent_with_steps(steps).communicate("x", iteration=1)).record
    ls = next(c for c in tr.commands if c.tool_name == "LS")
    assert ls.parameters == {"path": "/work"}  # renamed, results stripped
    web = next(c for c in tr.commands if c.tool_name == "WebSearch")
    assert web.parameters == {"query": "uipath"}  # summary (result) stripped
    # The leaked-result needle must NOT survive into any parameter value.
    assert all("skills/uipath-agents/" not in str(v) for c in tr.commands for v in c.parameters.values())


async def test_communicate_records_tool_error_from_nonzero_exit():
    steps = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "false", "exit_code": 1, "combined_output": "boom"})],
            usage=_usage(500, 0, 5, 0),
        ),
        _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(510, 0, 3, 0)),
    ]
    tr = (await _agent_with_steps(steps).communicate("run it", iteration=1)).record
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "error"


async def test_communicate_crash_returns_a_crashed_outcome():
    """A mid-stream SDK error returns a CRASHED outcome carrying a crashed partial record."""

    class _Boom:
        last_response = ""

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover - makes this an async generator

    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_Boom(), is_started=True)

    outcome = await agent.communicate("x", iteration=1)

    assert outcome.status is AgentEndStatus.CRASHED
    assert outcome.error == "Antigravity turn failed: kaboom"
    assert outcome.record.crashed is True
    assert outcome.record.result_summary is None


async def test_communicate_timeout_returns_a_timeout_outcome(monkeypatch):
    """A turn timeout returns a TIMEOUT outcome carrying a crashed partial record.

    Drives the timeout branch deterministically: a fake watchdog fires its
    ``on_timeout`` callback synchronously on entry (setting ``decoder.timeout_hit``,
    exactly what the real watchdog thread does), and the step pump then surfaces
    the cancel as ``asyncio.CancelledError`` — which ``run_with_watchdog`` turns
    into ``WatchdogFired``.
    """
    monkeypatch.setattr(_WATCHDOG, _FiringWatchdog)

    class _Cancelled:
        last_response = ""

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_Cancelled(), is_started=True)

    outcome = await agent.communicate("x", iteration=1, timeout=30.0)

    assert outcome.status is AgentEndStatus.TIMEOUT
    assert outcome.record.crashed is True


async def test_a_real_watchdog_timeout_returns_timeout_and_leaves_the_caller_uncancelled():
    """The real watchdog cancels the turn's CHILD task, never the caller.

    A harness that never yields a step past a 0.2 s budget: the outcome is
    ``TIMEOUT``, and the task that awaited ``communicate`` has no pending cancel
    request, so the orchestrator's next await is not torn down by a stray cancel.
    """

    class _Hangs:
        last_response = ""

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover - makes this an async generator

        async def cancel(self):
            return None

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_Hangs(), is_started=True)

    outcome = await asyncio.wait_for(agent.communicate("x", iteration=1, timeout=0.2), timeout=10)

    assert outcome.status is AgentEndStatus.TIMEOUT
    assert outcome.record.crashed is True
    caller = asyncio.current_task()
    assert caller is not None and caller.cancelling() == 0
    await asyncio.sleep(0)  # a stray cancel would land on this await


async def test_an_exception_after_a_requested_stop_ends_with_the_stop_status():
    """Closing the step stream after a stop can raise; the stopped turn still ends clean.

    The generator's own cleanup raises when ``_drain``'s ``aclosing`` closes it
    after the ``should_stop`` break — a pulled-step ``RuntimeError`` that is not
    retried. The turn was already over by request, so the outcome carries the
    stop's status and no error, not ``CRASHED``.
    """

    class _RaisesOnClose:
        last_response = ""

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            try:
                yield _step(
                    "TEXT_RESPONSE", "DONE", content="partial", content_delta="partial", usage=_usage(5, 0, 1, 0)
                )
                yield _step("TEXT_RESPONSE", "DONE", content="never pulled")
            finally:
                raise RuntimeError("aclose boom")

        async def cancel(self):
            return None

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_RaisesOnClose(), is_started=True)

    outcome = await agent.communicate("x", iteration=1, should_stop=lambda: StopReason.TOKEN_BUDGET)

    assert outcome.status is AgentEndStatus.TOKEN_BUDGET_EXCEEDED
    assert outcome.error is None
    assert outcome.record.crashed is False
    assert outcome.record.agent_output == "partial"


async def test_communicate_requires_started_agent():
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    with pytest.raises(RuntimeError, match="not started"):
        await agent.communicate("x", iteration=1)


def _install_fake_sdk(monkeypatch, sdk_agent_cls) -> None:
    """Stub ``google.antigravity`` in sys.modules so ``start()`` runs without the extra.

    ``LocalAgentConfig`` becomes a SimpleNamespace factory, so a test can assert on
    exactly the kwargs the agent built (``env``, ``policies``, ``capabilities``, ...).
    """
    ag = ModuleType("google.antigravity")
    ag.Agent = sdk_agent_cls
    ag.LocalAgentConfig = lambda **kwargs: SimpleNamespace(models=[], **kwargs)
    ag.types = SimpleNamespace(
        ThinkingLevel=lambda level: level,
        GeminiAPIEndpoint=type("GeminiAPIEndpoint", (), {}),
        GeminiModelOptions=SimpleNamespace,
    )
    hooks = ModuleType("google.antigravity.hooks")
    hooks.policy = SimpleNamespace(
        allow_all=lambda: SimpleNamespace(kind="allow_all"),
        deny_all=lambda: SimpleNamespace(kind="deny_all"),
        deny=lambda tool, **kw: SimpleNamespace(kind="deny", tool=tool),
        allow=lambda tool, **kw: SimpleNamespace(kind="allow", tool=tool),
    )
    google_pkg = sys.modules.get("google") or ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.antigravity", ag)
    monkeypatch.setitem(sys.modules, "google.antigravity.hooks", hooks)


# --- background-task poll loop (wait_for_wakeup is a dead stub on the Local ------
# harness; see antigravity_agent.py's communicate() comment + the plan for the
# full evidence trail. The model leaves a tool call open (never DONE) when it
# backgrounds work and goes idle -- these tests drive that signal directly. ------


def test_has_orphaned_tool_call_detects_active_vs_other_statuses():
    """Allowlist on ACTIVE, not a denylist on "not closed": a tool stuck in
    WAITING_FOR_USER/CANCELED/UNKNOWN is also never added to _closed_tools
    (that set only tracks DONE/ERROR), but must NOT be treated as pollable —
    it will never become DONE on its own (Phase-2-review finding). Layered on
    top: a cid already in _closed_tools is never orphaned even if its last-seen
    status were ever left at ACTIVE by a re-emission (final-review finding)."""
    state = _AntigravityDecoder.__new__(_AntigravityDecoder)
    state._closed_tools = set()
    state._tool_last_status = {}
    assert state.has_orphaned_tool_call() is False  # no tool calls at all

    state._tool_last_status = {"t1": "ACTIVE"}
    assert state.has_orphaned_tool_call() is True  # genuinely still running

    state._tool_last_status = {"t1": "DONE"}
    assert state.has_orphaned_tool_call() is False  # closed normally

    for stuck_status in ["WAITING_FOR_USER", "CANCELED", "UNKNOWN"]:
        state._tool_last_status = {"t1": stuck_status}
        assert state.has_orphaned_tool_call() is False, (
            f"a tool stuck in {stuck_status} must not trigger polling -- it will never become DONE"
        )

    # A second tool call still ACTIVE is enough, even if the first is DONE.
    state._tool_last_status = {"t1": "DONE", "t2": "ACTIVE"}
    assert state.has_orphaned_tool_call() is True

    # A closed cid stuck at ACTIVE (e.g. a stale re-emission) must not re-arm the
    # poll loop -- closure is authoritative over the last-seen status.
    state._closed_tools = {"t1"}
    state._tool_last_status = {"t1": "ACTIVE"}
    assert state.has_orphaned_tool_call() is False


async def test_communicate_fast_path_when_no_orphaned_tools(monkeypatch):
    """A normal turn closes its tool call before the stream exhausts -- the poll
    loop's condition is False on first check, so it's never entered: exactly one
    receive_steps() call, no sleep, byte-identical to today's behavior."""
    from coder_eval.agents import antigravity_agent

    async def _sleep_should_not_be_called(_seconds: float) -> None:
        raise AssertionError("asyncio.sleep must not be called on the no-orphan fast path")

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _sleep_should_not_be_called)

    steps = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "echo hi", "exit_code": 0, "combined_output": "hi"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    agent = _agent_with_steps(steps)
    tr = (await agent.communicate("run it", iteration=1)).record

    conv = agent._sdk_agent.conversation
    assert conv.receive_steps_call_count == 1
    assert tr.agent_output == "done"


async def test_communicate_does_not_poll_a_tool_stuck_waiting_for_user(monkeypatch):
    """A tool call whose LAST status is WAITING_FOR_USER (not ACTIVE) is never
    added to _closed_tools (that set only tracks DONE/ERROR) -- but it must
    NOT be mistaken for a genuine backgrounded job either, since a headless
    eval run will never actually answer the question. This is the exact gap a
    final cross-cutting review found: has_orphaned_tool_call must allowlist
    ACTIVE specifically, not just check "not yet closed"."""
    from coder_eval.agents import antigravity_agent

    async def _sleep_should_not_be_called(_seconds: float) -> None:
        raise AssertionError("asyncio.sleep must not be called for a tool stuck WAITING_FOR_USER")

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _sleep_should_not_be_called)

    steps = [
        _step(
            "TOOL_CALL",
            "WAITING_FOR_USER",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("ask_question", "t1", {"question": "which region?"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="waiting on you", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    agent = _agent_with_steps(steps)
    tr = (await agent.communicate("do it", iteration=1)).record

    conv = agent._sdk_agent.conversation
    assert conv.receive_steps_call_count == 1  # poll loop never entered
    ask = next(c for c in tr.commands if c.tool_name == "AskUser")
    assert ask.result_status == "unknown"  # force-closed as UNRESOLVED by finalize(), not polled forever


async def test_communicate_polls_and_resumes_after_orphaned_tool_closes(monkeypatch):
    """The model backgrounds a run_command and goes idle -- the tool call stays
    ACTIVE (never DONE) even past the final TEXT_RESPONSE. The orphaned-tool
    signal triggers a poll; the second receive_steps() call closes the tool and
    delivers the real result."""
    from coder_eval.agents import antigravity_agent

    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _record_sleep)

    batch1 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bg1", {"command_line": "sleep 12 && echo done"})],
        ),
        _step(
            "TEXT_RESPONSE",
            "DONE",
            content="I've started this in the background.",
            content_delta="I've started this in the background.",
            complete=True,
            usage=_usage(100, 0, 10, 0),
        ),
    ]
    batch2 = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc(
                    "run_command",
                    "bg1",
                    {"command_line": "sleep 12 && echo done", "exit_code": 0, "combined_output": "done"},
                )
            ],
            usage=_usage(50, 0, 5, 0),
        ),
        _step(
            "TEXT_RESPONSE",
            "DONE",
            content="All finished.",
            content_delta="All finished.",
            complete=True,
            usage=_usage(60, 0, 8, 0),
        ),
    ]
    agent = _agent_with_steps([batch1, batch2])
    tr = (await agent.communicate("do it", iteration=1)).record

    assert sleep_calls == [antigravity_agent._BACKGROUND_POLL_INTERVAL_SECONDS]
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "success"
    assert "All finished." in tr.agent_output
    assert agent._sdk_agent.conversation.receive_steps_call_count == 2


async def test_communicate_resolves_backgrounded_tool_call_with_no_id(monkeypatch):
    """The SDK types ToolCall.id as optional; the fallback synthetic id must be
    stable across a step's own ACTIVE -> DONE re-emission (same step_index), not
    derived from a mutable counter -- otherwise the DONE step mints a fresh id
    and the ACTIVE entry is orphaned forever, stalling the poll loop for its
    full budget on every id-less turn (final-review finding)."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)

    batch1 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", None, {"command_line": "sleep 12 && echo done"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="started", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    batch2 = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc(
                    "run_command",
                    None,
                    {"command_line": "sleep 12 && echo done", "exit_code": 0, "combined_output": "done"},
                )
            ],
        ),
        _step("TEXT_RESPONSE", "DONE", content="All finished.", complete=True, usage=_usage(5, 0, 1, 0)),
    ]
    agent = _agent_with_steps([batch1, batch2])
    tr = (await agent.communicate("do it", iteration=1)).record

    assert agent._sdk_agent.conversation.receive_steps_call_count == 2  # closed on the first poll, not the cap
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "success"
    assert len(tr.commands) == 1  # the id-less ACTIVE and DONE steps collapsed to ONE tool call, not two


async def test_id_less_tool_calls_in_different_trajectories_do_not_collide():
    """step_index is only unique WITHIN a trajectory -- the SDK itself keys step
    tracking on (trajectory_id, step_index), since a sub-agent trajectory can
    reuse the same low step_index values as the main one. Two id-less tool
    calls sharing a step_index but in DIFFERENT trajectories must still mint
    distinct fallback cids and produce two separate commands, not collapse
    into one (round-3 review finding); same trajectory + same step_index
    still collapses to one, as test_communicate_resolves_backgrounded_tool_call_with_no_id covers."""
    steps = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", None, {"command_line": "main job", "exit_code": 0, "output": "main"})],
            step_index=1,
            trajectory_id="",
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", None, {"command_line": "subagent job", "exit_code": 0, "output": "sub"})],
            step_index=1,  # same index as the step above, different trajectory
            trajectory_id="subagent-42",
        ),
        _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    agent = _agent_with_steps(steps)
    tr = (await agent.communicate("do two things", iteration=1)).record

    bash_calls = [c for c in tr.commands if c.tool_name == "Bash"]
    assert len(bash_calls) == 2  # distinct cids, not collapsed into one
    assert {c.tool_id for c in bash_calls} == {"run_command_1_0", "run_command_subagent-42:1_0"}


async def test_communicate_handles_two_sequential_background_jobs(monkeypatch):
    """paratransit-routing's real observed shape: the model backgrounds a job,
    it resolves, and the model immediately backgrounds a SECOND job before
    finally finishing -- the loop must not stop after just one poll cycle."""
    from coder_eval.agents import antigravity_agent

    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _record_sleep)

    batch1 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bgA", {"command_line": "job_a"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="started A", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    batch2 = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("run_command", "bgA", {"command_line": "job_a", "exit_code": 0, "combined_output": "a done"})
            ],
        ),
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bgB", {"command_line": "job_b"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="started B", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    batch3 = [
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("run_command", "bgB", {"command_line": "job_b", "exit_code": 0, "combined_output": "b done"})
            ],
        ),
        _step("TEXT_RESPONSE", "DONE", content="all done", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    agent = _agent_with_steps([batch1, batch2, batch3])
    tr = (await agent.communicate("do two things", iteration=1)).record

    assert len(sleep_calls) == 2  # exactly two poll cycles, one per backgrounded job
    bash_calls = [c for c in tr.commands if c.tool_name == "Bash"]
    assert len(bash_calls) == 2
    assert all(c.result_status == "success" for c in bash_calls)
    assert agent._sdk_agent.conversation.receive_steps_call_count == 3


async def test_communicate_stops_polling_at_max_poll_cap(monkeypatch):
    """A pathological, never-closing background job must not poll forever --
    the hard _MAX_BACKGROUND_POLLS cap bounds it independent of the turn budget."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent, "_MAX_BACKGROUND_POLLS", 3)
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _record_sleep)

    never_closing = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "stuck", {"command_line": "sleep 999999"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="waiting...", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    # A single batch that opens the orphan; every later call exhausts to an
    # empty batch (see _FakeConversation's docstring, matching the real SDK) --
    # the orphan is never closed, simulating a job whose state never changes.
    agent = _agent_with_steps([never_closing])
    tr = (await agent.communicate("do it forever", iteration=1)).record

    assert len(sleep_calls) == 3  # exactly _MAX_BACKGROUND_POLLS, not infinite
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "unknown"  # force-closed as UNRESOLVED by finalize()


async def test_communicate_finalizes_gracefully_under_a_realistic_turn_timeout(monkeypatch):
    """A never-resolving orphan under a REALISTIC configured timeout (300s, the
    framework's own experiments/default.yaml turn_timeout) must finalize through
    the poll loop's own graceful path -- force-close the orphan, grade normally
    -- instead of the ThreadedWatchdog cutting the whole turn at `timeout` first.

    Pre-fix, `_MAX_BACKGROUND_POLLS * _BACKGROUND_POLL_INTERVAL_SECONDS` (120 *
    5s = 600s) was DOUBLE the 300s default, so the watchdog always won that race
    and this exact scenario -- a tool call spuriously left ACTIVE with no real
    background job behind it, confirmed live in the final validation run -- burned
    the full turn timeout and crashed as TurnTimeoutError with zero criteria
    graded, a strict regression versus the pre-fix immediate finalize. Deriving
    the poll deadline from a FRACTION of the real `timeout` (not a disconnected
    cycle count) fixes it: the loop now exits through its own graceful path with
    room to spare before the watchdog's harder cutoff would ever fire."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)

    # Fake clock: turn_start_time=0.0, then +130s per subsequent call. The poll
    # loop reads time.monotonic() at least twice per iteration (the while-head
    # check, then the post-sleep deadline check), so this crosses the 240s
    # deadline (0.8 * 300s) after exactly one poll cycle -- proving the exit is
    # driven by the deadline, not by exhausting all 120 cycles. Scoped to the
    # adapter module's `time`: patching the shared `time.monotonic` also moves the
    # event loop's clock, and the turn body now runs in a child task on that loop.
    clock = iter([0.0, 130.0, 260.0])
    monkeypatch.setattr(antigravity_agent, "time", SimpleNamespace(monotonic=lambda: next(clock, 1_000_000.0)))

    never_closing = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "stuck", {"command_line": "sleep 999999"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="waiting...", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    agent = _agent_with_steps([never_closing])

    tr = (await agent.communicate("do it forever", iteration=1, timeout=300.0)).record  # the real default turn_timeout

    # Finalized and graded -- no TIMEOUT outcome, no crash.
    assert tr is not None
    assert not tr.crashed
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "unknown"  # force-closed as UNRESOLVED by finalize()
    # Exited via the poll_deadline (well under the 120-cycle cap), matching a
    # real turn where the watchdog's 300s cutoff never gets the chance to fire.
    assert agent._sdk_agent.conversation.receive_steps_call_count < 5


class _WatchdogFiresLater:
    """Fake ThreadedWatchdog that does NOT fire on entry (unlike _FiringWatchdog
    above) -- the test calls ``fire()`` mid-poll-loop, simulating a real watchdog
    thread firing between poll cycles rather than before the turn even starts.
    ``fire()`` sets ``fired`` and runs ``on_timeout``, as the real timer thread does."""

    captured: "_WatchdogFiresLater | None" = None

    def __init__(self, *, on_timeout: Callable[[], None], **_kwargs):
        self._on_timeout = on_timeout
        self.fired = False
        _WatchdogFiresLater.captured = self

    @classmethod
    def fire(cls) -> None:
        assert cls.captured is not None
        cls.captured.fired = True
        cls.captured._on_timeout()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


async def test_communicate_poll_loop_exits_promptly_once_watchdog_flag_lands(monkeypatch):
    """A watchdog timeout landing BETWEEN poll cycles (decoder.timeout_hit flips
    to True while the loop is sleeping) must stop the loop on its next condition
    check, not burn through the rest of _MAX_BACKGROUND_POLLS waiting for a
    cancellation that may not land on this coroutine right away (final-review
    finding: the loop condition must read the flag the watchdog already set)."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent, "_MAX_BACKGROUND_POLLS", 50)
    monkeypatch.setattr(_WATCHDOG, _WatchdogFiresLater)

    sleep_calls: list[float] = []

    async def _fire_watchdog_on_second_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            _WatchdogFiresLater.fire()

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _fire_watchdog_on_second_sleep)

    never_closing = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "stuck", {"command_line": "sleep 999999"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="waiting...", complete=True, usage=_usage(10, 0, 1, 0)),
    ]

    agent = _agent_with_steps([never_closing])
    outcome = await agent.communicate("do it forever", iteration=1, timeout=30.0)

    assert outcome.status is AgentEndStatus.TIMEOUT
    # Stopped right after the sleep that flipped timeout_hit -- NOT the (patched) cap of 50.
    assert len(sleep_calls) == 2
    # 1 initial drain + 1 poll re-drain (after sleep #1) -- the mid-loop
    # `if decoder.timeout_hit: break` skips the re-drain that would otherwise
    # follow sleep #2, so no 3rd receive_steps() call happens.
    assert agent._sdk_agent.conversation.receive_steps_call_count == 2
    bash = next(c for c in outcome.record.commands if c.tool_name == "Bash")
    assert bash.result_status == "unknown"


async def test_communicate_respects_should_stop_during_poll(monkeypatch):
    """A cooperative-stop request arriving during the poll phase must be
    honored before the next re-drain, not ignored until the job finishes."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)

    batch1 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bg1", {"command_line": "sleep 999"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="started", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    batch2 = [  # must never be drained -- should_stop fires right after the sleep
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("run_command", "bg1", {"command_line": "sleep 999", "exit_code": 0, "combined_output": "x"})
            ],
        ),
    ]
    agent = _agent_with_steps([batch1, batch2])
    conv = agent._sdk_agent.conversation

    call_count = 0

    def should_stop() -> StopReason | None:
        nonlocal call_count
        call_count += 1
        # None for batch1's 2 steps; a reason on the post-sleep check
        return StopReason.EARLY_CRITERION if call_count > 2 else None

    await agent.communicate("do it", iteration=1, should_stop=should_stop)

    assert conv.receive_steps_call_count == 1  # the poll's re-drain never happened
    assert conv.cancel_call_count == 1


class _TwoLayerReentrancyGuardedConversation:
    """Faithfully mirrors the REAL SDK's two-generator-layer shape:
    ``Conversation.receive_steps()`` (the public method ``_drain()`` calls) is
    ITSELF an async generator that delegates to
    ``LocalConnection.receive_steps()`` (``async for step in
    self._connection.receive_steps(): yield step``, verified against the
    installed SDK) -- and the ``_is_receiving`` re-entrancy flag lives on that
    INNER, connection-layer generator, not the outer one. A single-layer fake
    (putting the flag directly on the generator ``_drain()`` iterates) cannot
    catch a bug in how the outer/inner boundary is handled, since aclose()-ing
    a generator always closes ITSELF -- the question this fake exists to probe
    is whether that also reaches the inner one, and (confirmed live against
    real asyncio semantics) it does NOT do so synchronously: a `GeneratorExit`
    thrown into a delegating generator's frame does not immediately run the
    generator it was mid-iterating -- that's deferred to the event loop's
    async-gen finalizer, exactly like the original single-layer bug, just one
    level down. ``_drain()``'s fix is therefore a bounded retry (yielding via
    ``asyncio.sleep(0)`` for that already-scheduled finalizer to land), not a
    claim that the inner generator closes synchronously."""

    last_response = ""

    def __init__(self, batches):
        self._batches = list(batches)
        self._batch_index = 0
        self._is_receiving = False  # lives on the "connection" layer, like the real SDK
        self.receive_steps_call_count = 0

    async def send(self, prompt, **kwargs):
        return None

    async def _connection_receive_steps(self):
        if self._is_receiving:
            raise RuntimeError("Concurrent receive_steps() calls are not supported on this connection.")
        self._is_receiving = True
        try:
            batch = self._batches[self._batch_index] if self._batch_index < len(self._batches) else []
            self._batch_index += 1
            for s in batch:
                yield s
        finally:
            self._is_receiving = False

    async def receive_steps(self):
        # The "Conversation" layer: delegates to the connection layer exactly
        # like the real SDK's Conversation.receive_steps() does.
        self.receive_steps_call_count += 1
        async for step in self._connection_receive_steps():
            yield step

    async def cancel(self):
        return None


async def test_communicate_recovers_from_transient_reentrancy_after_cooperative_stop():
    """A cooperative-stop break on a PRIOR communicate() call can leave the
    real SDK's inner (connection-layer) generator not-yet-closed for a short
    window, since asyncio's async-gen finalizer runs it on a LATER event-loop
    turn, not synchronously when the outer generator is aclose()'d (confirmed
    live against the real two-layer delegation shape -- round-3 review finding;
    see _drain()'s docstring). The NEXT communicate() call must recover by
    retrying past that window (mirroring the SDK's own Conversation.send()
    handling of this exact RuntimeError) instead of crashing with
    a CRASHED outcome."""

    batch1 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bg1", {"command_line": "sleep 999"})],
        ),
        _step("TEXT_RESPONSE", "DONE", content="started", complete=True, usage=_usage(10, 0, 1, 0)),
    ]
    batch2 = [
        _step("TEXT_RESPONSE", "DONE", content="second turn", complete=True, usage=_usage(5, 0, 1, 0)),
    ]
    conversation = _TwoLayerReentrancyGuardedConversation([batch1, batch2])
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=conversation, is_started=True)

    # breaks after the first step
    await agent.communicate("do it", iteration=1, should_stop=lambda: StopReason.EARLY_CRITERION)

    # Without the retry, this second call crashes wrapping the fake's
    # RuntimeError (verified live before the fix landed). With it, the
    # transient window clears within a couple of asyncio.sleep(0) yields and
    # the second turn's real content is delivered, not silently dropped.
    tr = (await agent.communicate("do it again", iteration=2)).record
    assert tr.agent_output == "second turn"


async def test_a_failed_harness_teardown_is_logged_and_stop_still_completes(caplog):
    """The SDK's exit stack pops each callback before running it, so a failed close
    cannot be retried; it must at least be visible, since the harness may be left running."""
    import logging
    from contextlib import AsyncExitStack

    closes = 0

    async def _failing_close(*_exc: object) -> None:
        nonlocal closes
        closes += 1
        raise OSError("harness did not exit")

    stack = AsyncExitStack()
    stack.push_async_exit(_failing_close)
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent._exit_stack = stack
    agent._sdk_agent = SimpleNamespace(conversation=None, is_started=True)

    with caplog.at_level(logging.WARNING):
        await agent.stop()
        await agent.stop()

    assert closes == 1
    assert "harness did not exit" in caplog.text
    assert agent.get_state() == agent_module.AgentState.FINISHED


async def test_a_runtime_error_after_a_step_is_not_retried_as_reentrancy(monkeypatch):
    """Only an error raised before the first step is the re-entrancy window. A
    RuntimeError while processing a pulled step is a real failure: retrying it would
    re-pull the stream and emit the same steps again."""
    conversation_pulls = 0

    class _Conversation:
        last_response = ""

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            nonlocal conversation_pulls
            conversation_pulls += 1
            yield _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(5, 0, 1, 0))

        async def cancel(self):
            return None

    def _boom(self, step):
        raise RuntimeError("reducer bug")

    monkeypatch.setattr(agent_module._AntigravityDecoder, "__call__", _boom)
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = __import__("pathlib").Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_Conversation(), is_started=True)

    outcome = await agent.communicate("do it", iteration=1)

    assert outcome.status is AgentEndStatus.CRASHED
    assert outcome.error is not None and "reducer bug" in outcome.error
    assert conversation_pulls == 1


async def test_communicate_poll_budget_exhausted_finalizes_via_existing_timeout_path(monkeypatch):
    """A watchdog timeout landing during the poll loop's re-drain (not the first
    drain) must return a TIMEOUT outcome via the SAME watchdog branch -- the poll
    loop must not create a second, inconsistent timeout path.

    Uses ``_WatchdogFiresLater`` (not ``_FiringWatchdog``, which fires at entry
    and would make the loop's head condition skip the poll cycle entirely, per
    round-3 review) so ``decoder.timeout_hit`` only flips once a re-drain is
    genuinely in flight -- mirroring the real watchdog, whose ``on_timeout``
    callback and the ``CancelledError`` it triggers are the same causal event,
    not two independently-timed ones."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(_WATCHDOG, _WatchdogFiresLater)

    class _FiresWatchdogThenCancelsOnSecondDrain:
        last_response = ""

        def __init__(self) -> None:
            self.call_count = 0

        async def send(self, prompt, **kwargs):
            return None

        async def receive_steps(self):
            self.call_count += 1
            if self.call_count == 1:
                yield _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("run_command", "bg1", {"command_line": "sleep 999"})],
                )
                yield _step("TEXT_RESPONSE", "DONE", content="started", complete=True, usage=_usage(10, 0, 1, 0))
            else:
                _WatchdogFiresLater.fire()
                raise asyncio.CancelledError
                yield  # pragma: no cover - makes this an async generator

        async def cancel(self):
            return None

    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    conversation = _FiresWatchdogThenCancelsOnSecondDrain()
    agent._sdk_agent = SimpleNamespace(conversation=conversation, is_started=True)

    outcome = await agent.communicate("x", iteration=1, timeout=30.0)

    assert outcome.status is AgentEndStatus.TIMEOUT
    assert conversation.call_count == 2  # the re-drain genuinely ran, not skipped
    assert outcome.record.crashed is True
    bash = next(c for c in outcome.record.commands if c.tool_name == "Bash")
    assert bash.result_status == "unknown"


# --- env_path_prepend / mock-CLI PATH shadowing -----------------------------------
#
# The mock dirs reach the localharness through the SDK's per-agent ``env`` seam
# (LocalAgentConfig.env), which the SDK merges over os.environ at Popen time. Mock
# CLIs shadow real ones only if those dirs sit at the FRONT of the merged PATH, so
# an inverted join order (mocks at the back) must fail here. The process env is
# never mutated, which is what lets two tasks start harnesses concurrently.


async def test_harness_env_prepends_path_in_order(monkeypatch):
    """Mock dirs land at the FRONT of the overlay PATH, in order, ahead of the parent's."""
    monkeypatch.setenv("PATH", "/parent/bin")
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent._env_path_prepend = ["/sandbox/mocks", "/sandbox/bins"]

    assert agent._harness_env() == {"PATH": f"/sandbox/mocks{os.pathsep}/sandbox/bins{os.pathsep}/parent/bin"}


async def test_harness_env_none_without_prepend(monkeypatch):
    """No mock dirs → no overlay at all, so the SDK spawns with a plain inherited env."""
    monkeypatch.setenv("PATH", "/parent/bin")
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))

    assert agent._harness_env() is None


async def test_harness_env_never_mutates_process_env(monkeypatch):
    """Building the overlay leaves os.environ untouched — the whole point of the seam."""
    monkeypatch.setenv("PATH", "/parent/bin")
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent._env_path_prepend = ["/sandbox/mocks"]

    agent._harness_env()

    assert os.environ["PATH"] == "/parent/bin"


def test_installed_sdk_still_exposes_the_env_seam():
    """Pin the SDK-side half of the contract the rest of this section fakes.

    Every other env test stubs ``LocalAgentConfig``, so they prove only that we
    build the right kwarg. If a future ``google-antigravity`` bump dropped or
    renamed ``env``, all of them would still pass while mock CLIs silently
    stopped shadowing and the agent called the real tool instead — the exact
    silent-wrong-mode this seam exists to prevent. So assert against the real
    class: the field exists and round-trips.
    """
    config_mod = pytest.importorskip("google.antigravity.connections.local.local_connection_config")

    assert "env" in config_mod.LocalAgentConfig.model_fields
    cfg = config_mod.LocalAgentConfig(env={"PATH": "/sandbox/mocks:/usr/bin"})
    assert cfg.env == {"PATH": "/sandbox/mocks:/usr/bin"}
    # Omitted must stay None, not {} — the connection reads `is not None` to decide
    # whether to build a merged env at all, so {} would spawn with a rebuilt env
    # for every task instead of plain inheritance.
    assert config_mod.LocalAgentConfig().env is None


async def test_harness_env_resolves_path_key_case_insensitively(monkeypatch):
    """A non-uppercase PATH key (e.g. Windows 'Path') is reused, so the merge overrides it.

    The SDK merges as ``{**os.environ, **env}``; keying the overlay 'PATH' against an
    inherited 'Path' would add a sibling entry and leave the real PATH in force.
    """
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.os, "environ", {"Path": "/parent/bin"})
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent._env_path_prepend = ["/sandbox/mocks"]

    assert agent._harness_env() == {"Path": f"/sandbox/mocks{os.pathsep}/parent/bin"}


async def test_harness_env_handles_absent_path(monkeypatch):
    """When PATH is unset, the overlay is just the mock dirs (no stray separator tail)."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.os, "environ", {})
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent._env_path_prepend = ["/sandbox/mocks"]

    assert agent._harness_env() == {"PATH": f"/sandbox/mocks{os.pathsep}"}


async def test_concurrent_starts_get_isolated_mock_dirs(monkeypatch, tmp_path):
    """Two agents starting concurrently each see ONLY their own mock dirs.

    The defect this replaces: with a process-wide PATH mutation, agent B's harness
    could spawn inside agent A's mutated-PATH window and resolve run_command against
    A's mock CLIs for B's entire session. With the per-agent env seam the two configs
    are independent, so overlapping starts cannot contaminate each other.
    """
    monkeypatch.setenv("PATH", "/parent/bin")
    configs: list[Any] = []
    a_entered = asyncio.Event()

    class _FakeSdkAgent:
        def __init__(self, cfg):
            self._first = not configs
            configs.append(cfg)

        async def __aenter__(self):
            if self._first:
                # A parks inside its spawn so B's start() fully overlaps it.
                a_entered.set()
                await asyncio.sleep(0.05)
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    a = AntigravityAgent(parse_agent_config(type="antigravity"))
    b = AntigravityAgent(parse_agent_config(type="antigravity"))

    task_a = asyncio.create_task(a.start(str(tmp_path), env_path_prepend=["/a/mocks"]))
    await a_entered.wait()
    await b.start(str(tmp_path), env_path_prepend=["/b/mocks"])
    # Bounded: if a start ever serializes behind the other again, fail the test
    # rather than hang the suite waiting for a task that will never finish.
    await asyncio.wait_for(task_a, timeout=10)

    envs = [c.env for c in configs]
    assert envs == [
        {"PATH": f"/a/mocks{os.pathsep}/parent/bin"},
        {"PATH": f"/b/mocks{os.pathsep}/parent/bin"},
    ]
    assert os.environ["PATH"] == "/parent/bin"  # process env untouched throughout


async def test_start_passes_env_path_prepend_to_sdk_config(monkeypatch, tmp_path):
    """start(env_path_prepend=[...]) reaches LocalAgentConfig.env, not the process env.

    The SDK is stubbed via sys.modules so this needs no google-antigravity install.
    """
    monkeypatch.setenv("PATH", "/parent/bin")
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    await agent.start(str(tmp_path), env_path_prepend=["/sandbox/mocks", "/sandbox/bins"])

    assert agent._env_path_prepend == ["/sandbox/mocks", "/sandbox/bins"]
    assert configs[0].env == {"PATH": f"/sandbox/mocks{os.pathsep}/sandbox/bins{os.pathsep}/parent/bin"}
    assert os.environ["PATH"] == "/parent/bin"  # never mutated


async def test_start_omits_env_when_no_mock_dirs(monkeypatch, tmp_path):
    """Without mock dirs the SDK gets env=None, so the harness inherits os.environ verbatim."""

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    configs: list[Any] = []
    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    await agent.start(str(tmp_path))

    assert configs[0].env is None


# --- permission_mode and tool fields --------------------------------------------------


def _agent(**cfg) -> AntigravityAgent:
    return AntigravityAgent(parse_agent_config(type="antigravity", **cfg))


def _policy_pairs(**cfg) -> list[tuple[str, str | None]]:
    policy = SimpleNamespace(
        allow_all=lambda: SimpleNamespace(kind="allow_all"),
        deny_all=lambda: SimpleNamespace(kind="deny_all"),
        deny=lambda tool: SimpleNamespace(kind="deny", tool=tool),
        allow=lambda tool: SimpleNamespace(kind="allow", tool=tool),
    )
    return [(p.kind, getattr(p, "tool", None)) for p in _agent(**cfg)._policies(policy)]


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, [("allow_all", None)]),
        ({"allowed_tools": ["Bash"]}, [("deny_all", None), ("allow", "finish"), ("allow", "run_command")]),
        ({"disallowed_tools": ["Bash"]}, [("allow_all", None), ("deny", "run_command")]),
        (
            {"permission_mode": "plan"},
            [("allow_all", None), ("deny", "create_file"), ("deny", "edit_file"), ("deny", "run_command")],
        ),
        ({"allowed_tools": ["Skill"]}, [("deny_all", None), ("allow", "finish")]),
        ({"allowed_tools": []}, [("allow_all", None)]),
        (
            {"allowed_tools": ["Bash", "Read"], "disallowed_tools": ["Bash"]},
            [
                ("deny_all", None),
                ("allow", "finish"),
                ("allow", "run_command"),
                ("allow", "view_file"),
                ("deny", "run_command"),
            ],
        ),
    ],
)
def test_policies_map_the_uniform_fields(cfg: dict[str, Any], expected: list[tuple[str, str | None]]):
    assert _policy_pairs(**cfg) == expected


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions"])
async def test_non_plan_modes_stay_autonomous(monkeypatch, tmp_path, mode: str):
    """Only `plan` confines the harness; every other mode approves every call."""
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    await _agent(permission_mode=mode).start(str(tmp_path))

    assert [p.kind for p in configs[0].policies] == ["allow_all"]


async def test_start_hands_the_policies_to_the_sdk(monkeypatch, tmp_path):
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    await _agent(allowed_tools=["Bash"], permission_mode="plan").start(str(tmp_path))

    assert [(p.kind, getattr(p, "tool", None)) for p in configs[0].policies] == [
        ("deny_all", None),
        ("allow", "finish"),
        ("allow", "run_command"),
        ("deny", "create_file"),
        ("deny", "edit_file"),
        ("deny", "run_command"),
    ]


def test_tool_names_cover_the_canonical_vocabulary():
    from coder_eval.models import CANONICAL_TOOL_NAMES

    assert AntigravityAgent.tool_names is not None
    assert set(AntigravityAgent.tool_names.names) == CANONICAL_TOOL_NAMES
    assert "Finish" not in AntigravityAgent.tool_names.names
    assert AntigravityAgent.tool_names.names["Bash"] == ("run_command",)


# --- should_stop reasons -------------------------------------------------------------
#
# The adapter owns no cap. A `should_stop` reason ends the step loop at that boundary,
# and the reason picks the end status through `end_status_for`; the turn ends clean.


def _tool_steps(count: int) -> list:
    """`count` complete tool calls, each an ACTIVE step followed by its DONE step."""
    steps = []
    for i in range(count):
        call = _tc("run_command", f"t{i}", {"command_line": f"echo {i}"})
        steps.append(_step("TOOL_CALL", "ACTIVE", target="TARGET_ENVIRONMENT", tool_calls=[call]))
        done = _tc("run_command", f"t{i}", {"command_line": f"echo {i}", "exit_code": 0, "combined_output": str(i)})
        steps.append(_step("TOOL_CALL", "DONE", target="TARGET_ENVIRONMENT", tool_calls=[done]))
    return steps


class _EndCapture:
    """Stream callback that keeps the ``AgentEndEvent``."""

    def __init__(self) -> None:
        self.end: AgentEndEvent | None = None

    def on_event(self, event: object) -> None:
        if isinstance(event, AgentEndEvent):
            self.end = event


@pytest.mark.parametrize(
    ("reason", "status", "exhausted"),
    [
        (StopReason.TOOL_CALL_CAP, AgentEndStatus.TOOL_CALLS_EXHAUSTED, True),
        (StopReason.TOKEN_BUDGET, AgentEndStatus.TOKEN_BUDGET_EXCEEDED, False),
    ],
)
async def test_should_stop_reason_ends_the_turn_with_its_status(reason, status, exhausted):
    """A reason after the first processed step ends the loop; nothing further is pulled."""
    agent = _agent_with_steps(_tool_steps(5))
    capture = _EndCapture()

    record = (await agent.communicate("go", iteration=1, stream_callback=capture, should_stop=lambda: reason)).record

    assert capture.end is not None
    assert capture.end.status is status
    assert record.crashed is False
    assert record.tool_calls_exhausted is exhausted
    assert len(record.commands) == 1
    assert agent._sdk_agent.conversation.cancel_call_count == 1


async def test_stop_after_a_done_step_keeps_the_deciding_call_whole():
    """A stop polled after the call's DONE step keeps its result."""
    agent = _agent_with_steps(_tool_steps(3))
    polls = 0

    def should_stop() -> StopReason | None:
        nonlocal polls
        polls += 1
        return StopReason.TOOL_CALL_CAP if polls >= 2 else None

    record = (await agent.communicate("go", iteration=1, should_stop=should_stop)).record

    assert len(record.commands) == 1
    assert record.commands[0].result_status == "success"
    assert record.commands[0].result_summary == "0"


async def test_no_reason_consumes_every_step():
    agent = _agent_with_steps(_tool_steps(4))

    record = (await agent.communicate("go", iteration=1, should_stop=lambda: None)).record

    assert len(record.commands) == 4
    assert record.tool_calls_exhausted is False


async def test_cap_reached_on_a_poll_redrain_stops_polling(monkeypatch):
    """The cap and the background-poll loop share a boundary.

    A turn that backgrounds work drains, polls, and re-drains — so a poll cycle can
    be the cycle that reaches the cap. The re-drain honors it (the check lives in
    ``_drain``, which both paths call), and the loop must then stop polling rather
    than keep waiting out the background job on a run that is already over.
    """
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)

    bg = _tc("run_command", "bg1", {"command_line": "sleep 999"})
    batch1 = [_step("TOOL_CALL", "ACTIVE", target="TARGET_ENVIRONMENT", tool_calls=[bg])]
    # The re-drain kicks off a SECOND background job, then closes the first and runs
    # one more call — reaching the cap (2) with an orphan still ACTIVE. Both exit
    # conditions are live at once, and the cap has to win.
    batch2 = [
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "bg2", {"command_line": "sleep 999"})],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[
                _tc("run_command", "bg1", {"command_line": "sleep 999", "exit_code": 0, "combined_output": "x"})
            ],
        ),
        *_tool_steps(1),
    ]
    batch3 = _tool_steps(2)  # must never be drained
    agent = _agent_with_steps([batch1, batch2, batch3])
    conv = agent._sdk_agent.conversation
    monitor = TurnMonitor("t", [], limits=RunLimits(max_tool_calls=2))

    outcome = await agent.communicate("go", iteration=1, stream_callback=monitor, should_stop=monitor.should_stop)
    record = outcome.record

    assert monitor.stop_reason is StopReason.TOOL_CALL_CAP
    assert record.tool_calls_exhausted is True
    # The cap counts RESOLVED calls. The still-open bg2 is force-closed and recorded
    # as unresolved rather than dropped, so the trajectory shows what was interrupted.
    resolved = [c for c in record.commands if c.result_status != "unknown"]
    assert [c.tool_id for c in resolved] == ["bg1", "t0"]
    assert [c.tool_id for c in record.commands if c.result_status == "unknown"] == ["bg2"]
    assert conv.receive_steps_call_count == 2  # initial drain + one poll re-drain, then stop
    assert conv.cancel_call_count == 1


# ---------------------------------------------------------------------------
# Generation window
#
# Antigravity used to read datetime.now() ONCE per flush and pass it as both
# bounds with generation_duration_ms=0.0, so every task page reported 0ms of
# generation. The reducer now measures a real window and subtracts the tool
# executions that closed inside it — this harness interleaves tool calls into
# one generation, so a window legitimately contains time that is not model time.
# ---------------------------------------------------------------------------

_CLOCK_BASE = datetime(2026, 1, 1, 12, 0, 0)


def _at(ms: float) -> datetime:
    return _CLOCK_BASE + timedelta(milliseconds=ms)


def _replay(
    stream: list[Any],
    *,
    status: AgentEndStatus = AgentEndStatus.COMPLETED,
    reason: str | None = None,
    agent_output: str | None = None,
) -> tuple[Replay, _AntigravityDecoder]:
    """Drive an `_AntigravityDecoder` through `coder_eval.testing.replay` from `_CLOCK_BASE`.

    Opens the one inner turn `communicate` opens and ends through `decoder.end`;
    returns the decoder too, for the tests that pin its bookkeeping.
    """
    decoders: list[_AntigravityDecoder] = []

    def make(emitter: TurnEmitter) -> _AntigravityDecoder:
        decoder = _AntigravityDecoder(emitter)
        emitter.begin_inner_turn(decoder.turn_id)
        decoders.append(decoder)
        return decoder

    def end(decoder: _AntigravityDecoder) -> TurnOutcome:
        return decoder.end(status, reason=reason, agent_output=agent_output)

    result = replay(stream, make, clock=ScriptedClock(_CLOCK_BASE), model="gemini-3.5-flash", end=end)
    return result, decoders[0]


def _assistant(record):
    return [m for m in record.messages if m.role == "assistant"]


def _opening_step():
    """A MODEL step that seeds the first window's mark and adds no block."""
    return _step("THINKING", "ACTIVE", thinking="...")


def _thinking_done(text: str):
    return _step("THINKING", "DONE", thinking=text, usage=_usage(100, 0, 5, 5))


def _bash_active(*ids: str):
    calls = [_tc("run_command", tid, {"command_line": tid}) for tid in ids]
    return _step("TOOL_CALL", "ACTIVE", target="TARGET_ENVIRONMENT", tool_calls=calls)


def _bash_done(*ids: str):
    calls = [_tc("run_command", tid, {"command_line": tid, "exit_code": 0}) for tid in ids]
    return _step("TOOL_CALL", "DONE", target="TARGET_ENVIRONMENT", tool_calls=calls)


class TestBusyMs:
    """`busy_ms` is the UNION of tool intervals, clipped to the window.

    A scalar sum was wrong twice over: overlapping tools (this harness
    resolves several calls from one Step and backgrounds anything over ten
    seconds) get counted more than once, and a tool that opened before the
    window gets charged in full to it. Subtracting such a sum from a
    generation window understates generation and, with enough concurrency,
    drives it negative — reintroducing the 0.0 this change removes.
    """

    @staticmethod
    def _at(ms: float) -> datetime:
        return _CLOCK_BASE + timedelta(milliseconds=ms)

    def _busy(self, spans, lo=0, hi=10_000) -> float:
        from coder_eval.timing import busy_ms

        return busy_ms([(self._at(s), self._at(e)) for s, e in spans], self._at(lo), self._at(hi))

    def test_no_spans_is_zero(self):
        assert self._busy([]) == 0.0

    def test_a_single_span_is_its_own_length(self):
        assert self._busy([(100, 400)]) == 300.0

    def test_disjoint_spans_add(self):
        assert self._busy([(100, 200), (500, 700)]) == 300.0

    def test_overlapping_spans_count_once(self):
        # The defect: summing gives 400, but only 300ms of wall time was busy.
        assert self._busy([(100, 300), (200, 400)]) == 300.0

    def test_a_contained_span_adds_nothing(self):
        assert self._busy([(100, 900), (300, 400)]) == 800.0

    def test_adjacent_spans_merge_without_double_counting_the_seam(self):
        assert self._busy([(100, 200), (200, 300)]) == 200.0

    def test_input_order_does_not_matter(self):
        assert self._busy([(500, 700), (100, 300), (200, 400)]) == 500.0

    def test_a_span_is_clipped_to_the_window(self):
        # Opened before the window and closed after it: only the overlap counts.
        assert self._busy([(0, 5_000)], lo=1_000, hi=1_500) == 500.0

    def test_a_span_entirely_outside_the_window_is_dropped(self):
        assert self._busy([(0, 500)], lo=1_000, hi=2_000) == 0.0

    def test_a_zero_length_span_is_dropped(self):
        assert self._busy([(100, 100)]) == 0.0


async def test_concurrent_tools_do_not_over_subtract():
    """Overlapping tool calls are subtracted once, not once each.

    Four calls opened by one Step and closed by the next overlap entirely.
    Summing their durations exceeded the window and clamped
    `generation_duration_ms` to 0.0 — the pre-change symptom, with a 0%
    breakdown on the task page and nothing failing.
    """
    ids = ("t0", "t1", "t2", "t3")
    result, _ = _replay(
        [
            Tick(50),
            _opening_step(),
            Tick(100),
            _thinking_done("first"),
            Tick(200),
            _bash_active(*ids),
            Tick(600),
            _bash_done(*ids),
            Tick(1000),
            _thinking_done("second"),
        ]
    )
    record = result.record

    second = _assistant(record)[1]
    tools = record.commands
    assert len(tools) == 4
    span_ms = (second.completed_at - second.started_at).total_seconds() * 1000.0
    summed_ms = sum(c.duration_ms or 0.0 for c in tools)

    assert span_ms == pytest.approx(900.0)
    assert summed_ms > span_ms, "fixture must make the naive sum exceed the window"
    busy_ms = (
        max(c.execution_completed_at for c in tools) - min(c.execution_started_at for c in tools)
    ).total_seconds() * 1000.0
    assert busy_ms == pytest.approx(400.0)
    assert second.generation_duration_ms == pytest.approx(span_ms - busy_ms)


async def test_generation_window_is_measured_not_zero():
    """Every streaming generation reports a real, positive window.

    Asserts the PROPERTY, not a millisecond value — this runs on the real
    clock, so only the shape is deterministic.
    """
    steps = [
        _step("THINKING", "DONE", thinking="first", usage=_usage(100, 0, 5, 5)),
        _step("THINKING", "DONE", thinking="second", usage=_usage(120, 0, 6, 4)),
    ]
    record = (await _agent_with_steps(steps).communicate("go", iteration=1)).record

    messages = _assistant(record)
    assert len(messages) == 2
    for m in messages:
        assert m.generation_duration_ms is not None
        assert m.generation_duration_ms > 0
        assert m.started_at < m.completed_at


async def test_consecutive_windows_chain_end_to_start():
    """Message n+1 begins where message n ended — the mark IS the previous flush."""
    steps = [
        _step("THINKING", "DONE", thinking="a", usage=_usage(100, 0, 5, 5)),
        _step("THINKING", "DONE", thinking="b", usage=_usage(100, 0, 5, 5)),
        _step("TEXT_RESPONSE", "DONE", content="c", content_delta="c", complete=True, usage=_usage(100, 0, 5, 0)),
    ]
    record = (await _agent_with_steps(steps).communicate("go", iteration=1)).record

    messages = _assistant(record)
    assert len(messages) == 3
    for earlier, later in pairwise(messages):
        assert later.started_at == earlier.completed_at


async def test_tool_execution_is_subtracted_from_the_window():
    """A tool closing inside a generation is not counted as model time.

    This is the test that pins the design decision. Without it, "simplifying"
    the subtraction to a reset-on-tool-end passes everything else — and loses
    real model time, because a harness-local tool can close 8 ms after it opens
    while seconds of model time separate the two flushes around it.
    """
    result, _ = _replay(
        [
            Tick(50),
            _opening_step(),
            Tick(100),
            _thinking_done("plan"),
            Tick(300),
            _bash_active("t1"),
            Tick(400),
            _bash_done("t1"),
            Tick(1000),
            _step(
                "TEXT_RESPONSE",
                "DONE",
                content="done",
                content_delta="done",
                complete=True,
                usage=_usage(200, 0, 10, 0),
            ),
        ]
    )
    record = result.record

    messages = _assistant(record)
    assert len(messages) == 2
    second = messages[1]
    bash = next(c for c in record.commands if c.tool_name == "Bash")

    span_ms = (second.completed_at - second.started_at).total_seconds() * 1000.0
    assert bash.duration_ms == pytest.approx(100.0)
    assert second.generation_duration_ms == pytest.approx(span_ms - bash.duration_ms)
    assert span_ms == pytest.approx(900.0)
    assert second.generation_duration_ms == pytest.approx(800.0)


async def test_a_straddling_tool_is_charged_only_for_its_in_window_part():
    """A tool open across a flush is clipped to the window it is subtracted from.

    `t1` opens before the first flush and closes after it. Only the part that
    elapsed INSIDE the second window is not generation time there; charging
    its full duration would understate generation and, with a long enough
    overhang, drive the result to a clamped 0.0 — the very value this change
    exists to stop publishing.
    """
    result, _ = _replay(
        [
            Tick(100),
            _bash_active("t1"),  # opens here and stays open across the first flush
            Tick(200),
            _bash_active("t2"),  # opens and closes entirely inside the first window
            Tick(300),
            _bash_done("t2"),
            Tick(500),
            _thinking_done("first"),
            Tick(800),
            _bash_done("t1"),  # closes in the SECOND window, carrying the first window's overhang
            Tick(1000),
            _thinking_done("second"),
        ]
    )
    record = result.record

    second = _assistant(record)[1]
    slow = next(c for c in record.commands if c.tool_id == "t1")
    span_ms = (second.completed_at - second.started_at).total_seconds() * 1000.0
    in_window_ms = (slow.execution_completed_at - second.started_at).total_seconds() * 1000.0

    assert slow.duration_ms > span_ms, "fixture must produce a straddling tool"
    assert 0 < in_window_ms < slow.duration_ms, "part of the tool ran before this window"
    assert second.generation_duration_ms == pytest.approx(span_ms - in_window_ms)
    assert second.generation_duration_ms == pytest.approx(200.0)


async def test_a_tool_still_open_at_the_flush_is_not_generation_time():
    """The sibling of the straddle test above, for the window the tool opened IN.

    Subtracting only CLOSED intervals published the part of a still-running
    call that had already elapsed as model time, while the call's own
    `duration_ms` counted it again. These windows tile the turn, so there is no
    slack to absorb that: measured on tasks/hello_date with a live
    gemini-3.1-pro-preview, a Bash opening 1.7 ms before the flush drove
    Sum(generation) + Sum(command) 0.26 ms PAST the turn's own
    `duration_seconds`, on a turn whose entire headroom was 1.4 ms. Four
    sibling runs passed by 1.2-8.7 ms out of ~12 s, so it was a coin flip.
    """
    result, _ = _replay(
        [
            Tick(100),
            _opening_step(),
            Tick(200),
            _bash_active("t1"),  # STILL RUNNING when the first window is cut
            Tick(500),
            _thinking_done("first"),
            Tick(800),
            _bash_done("t1"),
            Tick(1000),
            _thinking_done("second"),
        ]
    )
    record = result.record

    first = _assistant(record)[0]
    slow = next(c for c in record.commands if c.tool_id == "t1")
    span_ms = (first.completed_at - first.started_at).total_seconds() * 1000.0
    in_window_ms = (first.completed_at - slow.execution_started_at).total_seconds() * 1000.0

    assert slow.execution_started_at < first.completed_at, "fixture must open the tool in this window"
    assert slow.execution_completed_at > first.completed_at, "...and leave it open across the flush"
    assert first.generation_duration_ms == pytest.approx(span_ms - in_window_ms)
    # The whole point: what the page shows as Generation plus what it shows as
    # Tool exec must still fit in the window they are shown against.
    assert first.generation_duration_ms + in_window_ms == pytest.approx(span_ms)


async def test_a_no_op_flush_does_not_move_the_mark():
    """An empty generation must leave the open window alone.

    The early return in `_flush_generation` sits before any mark handling, so
    a usage_metadata step carrying nothing must not restart the measurement —
    otherwise the real generation that follows reports only the time since the
    empty one.
    """
    real = _thinking_done("real")
    # Zero usage and no content: reaches the flush, appends nothing.
    empty = _step("THINKING", "DONE", usage=_usage(0, 0, 0, 0))

    without, _ = _replay([Tick(100), _opening_step(), Tick(1000), real])
    with_empty, decoder = _replay([Tick(100), _opening_step(), Tick(500), empty, Tick(1000), real])

    assert decoder.generations == 1
    with_messages = _assistant(with_empty.record)
    assert len(with_messages) == 1, "the empty generation must not produce a message"
    assert with_messages[0].started_at == _assistant(without.record)[0].started_at == _at(100)
    assert with_messages[0].generation_duration_ms == _assistant(without.record)[0].generation_duration_ms


async def test_generation_and_tool_time_account_for_the_turn():
    """Σ generation + tool union + head + tail tiles the turn's own bracket.

    Measured against the ``AgentStartEvent`` / ``AgentEndEvent`` stamps, the span
    the buckets are defined on. Not against ``duration_seconds``: the emitter reads
    that on a separate monotonic call before it stamps the end event, so on this
    sub-millisecond fake turn the bracket exceeds it by a few microseconds every
    time. Before the window existed the generation half was identically 0.

    The HEAD is part of the sum, and has to be: the first window now opens at
    the first observed `Step` rather than at turn entry, so the dispatch before
    it is a measured bucket instead of time hidden inside msg0's generation.
    Asserting `generation + tool` alone against a share of the turn was an
    assertion that the head stays empty — which is what this phase deliberately
    stopped being true.
    """
    steps = [
        _step("THINKING", "DONE", thinking="plan", usage=_usage(100, 0, 5, 5)),
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "ls"})],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "ls", "exit_code": 0})],
        ),
        _step(
            "TEXT_RESPONSE", "DONE", content="done", content_delta="done", complete=True, usage=_usage(200, 0, 10, 0)
        ),
    ]
    seen: list[Any] = []
    record = (
        await _agent_with_steps(steps).communicate(
            "go", iteration=1, stream_callback=SimpleNamespace(on_event=seen.append)
        )
    ).record

    gen_ms = sum(m.generation_duration_ms or 0.0 for m in _assistant(record))
    head_ms = record.harness_startup_ms or 0.0

    assert gen_ms > 0
    assert head_ms > 0, "the dispatch before the first Step is now a measured bucket, not 0.0"
    assert_identity_closes(
        record,
        started_at=next(e.timestamp for e in seen if isinstance(e, AgentStartEvent)),
        ended_at=next(e.timestamp for e in seen if isinstance(e, AgentEndEvent)),
    )

    # NO share-of-turn LOWER bound on generation. This case runs on the REAL
    # clock, where the fake conversation's own overhead lands in head and tail,
    # so any `>= share * turn_ms` assertion is a scheduler-noise detector. The
    # magnitudes are asserted on a scripted clock in
    # tests/test_timing_identity_contract.py.


async def test_timing_change_moves_no_token_bucket():
    """The window is three timing fields; the token buckets must not shift.

    Runs on a stream that also exercises the new timing, so a regression in
    `_flush_generation` shows up here as a token failure too.
    """
    steps = [
        _step("THINKING", "DONE", thinking="plan", usage=_usage(1000, 0, 10, 20)),
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "ls"})],
        ),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "t1", {"command_line": "ls", "exit_code": 0})],
            usage=_usage(1200, 0, 15, 5),
        ),
        _step(
            "TEXT_RESPONSE", "DONE", content="done", content_delta="done", complete=True, usage=_usage(1300, 0, 30, 0)
        ),
    ]
    record = (await _agent_with_steps(steps).communicate("go", iteration=1)).record

    assert record.token_usage is not None
    assert record.token_usage.output_tokens == (10 + 20) + (15 + 5) + (30 + 0)
    assert record.token_usage.uncached_input_tokens == 1000 + 1200 + 1300
    # The reconciliation invariant, via the shared SSOT helper rather than a
    # local re-implementation of two of its four buckets.
    assert_reconciliation(record.model_dump(mode="json"))
    assert all(m.generation_duration_ms is not None for m in _assistant(record))


def _plan_tool_second_stream() -> list[Any]:
    return [
        Tick(50),
        _opening_step(),
        Tick(100),
        _thinking_done("plan"),
        Tick(300),
        _bash_active("t1"),
        Tick(450),
        _bash_done("t1"),
        Tick(1000),
        _thinking_done("second"),
    ]


async def test_the_published_window_reconciles_to_its_own_bounds():
    """The reducer subtracted exactly the spans the record carries.

    `decompose_run.py` and the evalboard's Unaccounted cell both recompute the
    tool UNION from the recorded command spans and subtract it from the
    recorded window bounds, so this asserts the published window agrees with
    that same set.
    """
    from coder_eval.timing import busy_ms

    record = _replay(_plan_tool_second_stream())[0].record

    second = _assistant(record)[1]
    spans = [
        (c.execution_started_at, c.execution_completed_at)
        for c in record.commands
        if c.execution_started_at is not None and c.execution_completed_at is not None
    ]
    span_ms = (second.completed_at - second.started_at).total_seconds() * 1000.0
    expected = span_ms - busy_ms(spans, second.started_at, second.completed_at)
    assert second.generation_duration_ms == pytest.approx(expected)
    assert second.generation_duration_ms == pytest.approx(750.0)


async def test_the_window_is_measured_without_relying_on_the_negative_clamp():
    """A positive window, and no clamp underneath it.

    The span used to be read off `time.monotonic()` while the tool intervals
    were wall, so the two could disagree and drive the result negative; the
    clamp that caught it published a `0.0` indistinguishable from a real
    instant generation, and a debug line was the only trace. One basis makes
    that unrepresentable: `busy_ms` clips to the window and unions overlaps, so
    it cannot exceed a span derived from the same clock.
    """
    record = _replay(_plan_tool_second_stream())[0].record

    second = _assistant(record)[1]
    assert second.generation_duration_ms > 0.0
    assert second.completed_at > second.started_at
    # The branch and its debug line are deleted, not merely unreachable.
    source = inspect.getsource(agent_module)
    assert "Generation window went negative" not in source
    assert "_gen_mark_monotonic" not in source


async def test_each_turn_gets_a_fresh_clock():
    """A second turn on the same agent re-anchors rather than inheriting.

    One clock per turn is the rule: a clock outliving its turn would stamp the
    next one with the previous turn's wall origin, and over a long run would
    accumulate drift against real wall time.
    """
    step = _step("THINKING", "DONE", thinking="a", usage=_usage(100, 0, 5, 5))
    agent = _agent_with_steps([step])
    first = _assistant((await agent.communicate("go", iteration=1)).record)
    # The fake conversation yields one batch and is then spent, so borrow a
    # fresh one. The agent INSTANCE is deliberately the same: what is under
    # test is that its second turn builds its own clock rather than inheriting
    # the first turn's origin.
    agent._sdk_agent = _agent_with_steps([step])._sdk_agent
    second = _assistant((await agent.communicate("again", iteration=2)).record)

    assert first and second
    # Re-anchored: the later turn's window opens after the earlier one closed.
    assert second[0].started_at >= first[0].completed_at
    assert second[0].completed_at > second[0].started_at


class TestAntigravityFirstWindowReseed:
    """The first MODEL `Step` moves `_gen_mark`; a later one must not.

    Replayed on a scripted clock, NOT through `communicate()`: the fake
    conversation yields with no delay, so an end-to-end run cannot pin the
    MAGNITUDE — the two stamps land within microseconds of each other, so no
    assertion there could say the mark moved by the right amount.

    It can detect the mark moving at all, and does:
    `test_generation_and_tool_time_account_for_the_turn` asserts `head_ms > 0`
    and fails if the re-seed call is removed. These tests are the ones that say
    WHERE it moved to and that it moves only once.
    """

    class _Clock:
        def __init__(self) -> None:
            self.at_ms = 0.0

        def now(self) -> datetime:
            return _at(self.at_ms)

    def test_the_first_step_moves_the_mark_off_the_turn_entry_stamp(self):
        """Dispatch before the first Step is head, not the first generation.

        Before the re-seed the mark was stamped when the turn state was built,
        so this interval was published as generation — ~4.7 s per turn against
        a later-window median of 3.3 s.
        """
        _, decoder = _replay([Tick(900), _opening_step()])  # dispatch + TTFT

        assert decoder._first_output_seen is True
        assert decoder._gen_mark == _at(900)

    def test_a_later_step_does_not_move_it(self):
        """Re-seeding more than once per turn is the defect, not the feature."""
        _, decoder = _replay([Tick(900), _opening_step(), Tick(5000), _step("THINKING", "ACTIVE", thinking="more")])

        assert decoder._gen_mark == _at(900)

    def test_seeding_twice_by_hand_is_a_no_op_the_second_time(self):
        """The once-per-turn guard, stated outright rather than inferred."""
        clock = self._Clock()
        emitter = TurnEmitter(
            task_id="t",
            iteration=1,
            prompt="go",
            model="gemini-3.5-flash",
            basis=TimingBasis.TURN_CLOCK,
            clock=clock,
            sinks=[],
        )
        emitter.begin()
        decoder = _AntigravityDecoder(emitter)
        assert decoder._gen_mark == _CLOCK_BASE

        clock.at_ms = 900
        decoder._seed_first_generation_window("MODEL")
        clock.at_ms = 5000
        decoder._seed_first_generation_window("MODEL")

        assert decoder._gen_mark == _at(900)

    def test_a_flush_still_advances_the_mark_and_opens_at_the_reseeded_one(self):
        """The re-seed must not break the tiling it sits in front of."""
        result, decoder = _replay(
            [
                Tick(900),
                _step("THINKING", "ACTIVE", thinking="plan"),
                Tick(2000),
                _thinking_done("plan"),
            ]
        )

        message = _assistant(result.record)[0]
        assert message.started_at == _at(900), "opens at the RE-SEEDED mark"
        assert message.generation_duration_ms == pytest.approx(1100.0)
        assert decoder._gen_mark == _at(2000), "and the flush advances it"

    def test_a_non_model_step_does_not_seed_the_window(self):
        """The field is MODEL output, and the SDK streams Steps that are not.

        `StepSource` carries SYSTEM and USER besides MODEL, and the SDK's event
        processor queues every `step_update` verbatim, so a turn can open with
        one. Seeding on it would put the mark before the model spoke and hand
        the remainder back to msg0's generation — the defect being fixed.
        """
        system = _step("SYSTEM_MESSAGE", "DONE", source="SYSTEM", content="compacting")

        _, before = _replay([Tick(400), system])
        assert before._first_output_seen is False
        assert before._gen_mark == _CLOCK_BASE, "a system Step must not open the generation window"

        _, after = _replay([Tick(400), system, Tick(900), _opening_step()])
        assert after._gen_mark == _at(900), "the first MODEL Step does"

    def test_a_turn_that_streams_no_step_keeps_the_turn_entry_mark(self):
        _, decoder = _replay([])
        assert decoder._first_output_seen is False
        assert decoder._gen_mark == _CLOCK_BASE


class TestAntigravityDecoder:
    """`_AntigravityDecoder` over a real emitter: ids, parameters, orphans, tokens and replies."""

    def test_an_id_less_call_falls_back_to_a_trajectory_scoped_id(self):
        """`{name}_{trajectory}:{step_index}_{call_index}`, stable across ACTIVE -> DONE."""

        def calls(done: bool) -> list[Any]:
            extra = {"exit_code": 0} if done else {}
            return [
                _tc("run_command", None, {"command_line": "a", **extra}),
                _tc("view_file", None, {"file_path": "x.py"}),
            ]

        result, _ = _replay(
            [
                _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=calls(False),
                    step_index=3,
                    trajectory_id="traj",
                ),
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=calls(True),
                    step_index=3,
                    trajectory_id="traj",
                ),
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("run_command", None, {"command_line": "b", "exit_code": 0})],
                    step_index=4,
                ),
            ]
        )

        commands = {c.tool_id: c for c in result.record.commands}
        assert set(commands) == {"run_command_traj:3_0", "view_file_traj:3_1", "run_command_4_0"}
        assert all(c.result_status == "success" for c in commands.values())
        assert_stream_balanced(result.events)

    def test_parameters_keep_only_the_input_keys_seen_at_start(self):
        """A key first seen at DONE is the harness's result payload, whatever its name."""
        result, _ = _replay(
            [
                _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("run_command", "t1", {"command_line": "make", "cwd": "/w"})],
                ),
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[
                        _tc(
                            "run_command",
                            "t1",
                            {"command_line": "make", "cwd": "/w", "exit_code": 0, "elapsed": "3s"},
                        )
                    ],
                ),
                # First seen at DONE: only the static backstop can drop `summary`.
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("search_web", "t2", {"query": "q", "summary": "leaked"})],
                ),
            ]
        )

        starts = {e.tool.tool_id: e.tool.parameters for e in result.events if isinstance(e, ToolStartEvent)}
        assert starts["t1"] == {"command": "make", "cwd": "/w"}
        commands = {c.tool_id: c.parameters for c in result.record.commands}
        assert commands == {"t1": {"command": "make", "cwd": "/w"}, "t2": {"query": "q"}}

    def test_an_orphan_is_swept_unresolved_with_no_completion(self):
        """A call still ACTIVE at the end keeps its start and gains no end, duration or error."""
        result, _ = _replay(
            [
                Tick(100),
                _bash_active("bg1"),
                Tick(500),
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="backgrounded",
                    content_delta="backgrounded",
                    complete=True,
                    usage=_usage(90, 0, 10, 0),
                ),
            ]
        )

        [orphan] = result.record.commands
        assert orphan.result_status == "unknown"
        assert orphan.execution_started_at == _at(100)
        assert orphan.execution_completed_at is None
        assert orphan.duration_ms is None
        assert orphan.error_message is None
        ends = [e for e in result.events if isinstance(e, ToolEndEvent)]
        assert [e.status for e in ends] == [ToolEndStatus.UNRESOLVED]
        assert_stream_balanced(result.events)

    def test_generation_tokens_sum_to_the_turn_end_tokens(self):
        """One inner turn; its `TurnEndEvent.tokens` is the sum of the per-generation deltas."""
        result, decoder = _replay(
            [
                _step("THINKING", "DONE", thinking="first", usage=_usage(100, 0, 10, 5)),
                _step("THINKING", "DONE", thinking="second", usage=_usage(110, 20, 12, 6)),
                _step("TEXT_RESPONSE", "DONE", content="third", complete=True, usage=_usage(120, 0, 14, 0)),
            ]
        )

        [turn_end] = [e for e in result.events if isinstance(e, TurnEndEvent)]
        [agent_end] = [e for e in result.events if isinstance(e, AgentEndEvent)]
        tokens = turn_end.tokens
        assert tokens is not None
        messages = _assistant(result.record)
        assert len(messages) == decoder.generations == 3
        assert sum(m.input_tokens for m in messages) == tokens.uncached_input_tokens == 100 + 90 + 120
        assert sum(m.output_tokens for m in messages) == tokens.output_tokens == 15 + 18 + 14
        assert sum(m.cache_read_tokens for m in messages) == tokens.cache_read_input_tokens == 20
        assert sum(m.cache_creation_tokens for m in messages) == tokens.cache_creation_input_tokens == 0
        for bucket in ("uncached_input_tokens", "output_tokens", "cache_read_input_tokens"):
            assert getattr(agent_end.usage, bucket) == getattr(tokens, bucket)
        assert_stream_balanced(result.events)

    def test_a_user_source_text_step_is_not_assistant_text(self):
        """The prompt echo adds no text block, no text chunk and no `agent_output`; the reply does."""
        from tests._fixtures.golden_streams.antigravity_fixtures import ANTIGRAVITY_SCENARIOS

        steps = next(s for s in ANTIGRAVITY_SCENARIOS if s.name == "f_user_prompt_step").steps
        result, decoder = _replay(steps)

        texts = [b.text for m in _assistant(result.record) for b in m.content_blocks if b.block_type == "text"]
        assert texts == ["DONE."]
        assert decoder.output_parts == ["DONE."]
        assert [e.text for e in result.events if isinstance(e, TextChunkEvent)] == ["DONE."]
        assert result.record.agent_output == "DONE."
        assert result.record.result_summary is not None
        assert result.record.result_summary.result == "DONE."

    def test_a_user_source_delta_stays_out_of_a_failed_turns_output(self):
        """A failed turn keeps only completed reply text, so neither the prompt echo nor a partial delta lands."""
        result, _ = _replay(
            [
                _step("TEXT_RESPONSE", "DONE", source="USER", target="UNKNOWN", content="do it", content_delta="do it"),
                _step("TEXT_RESPONSE", "ACTIVE", content_delta="DO"),
            ],
            status=AgentEndStatus.CRASHED,
            reason="boom",
        )

        assert result.outcome.status is AgentEndStatus.CRASHED
        assert result.outcome.error == "boom"
        assert result.record.agent_output == ""
        assert [e.text for e in result.events if isinstance(e, TextChunkEvent)] == ["DO"]
        assert result.record.result_summary is None


class TestTheTurnBracketComesFromTheTurnClock:
    """The SOURCE of the two bracket stamps: the turn clock.

    This is the harness the defect was measured on. It holds its process across
    turns, so nothing happens between its last flush and its `AgentEndEvent`
    and its true tail is ~0.1 ms — the only scale at which the drift between a
    raw `datetime.now()` and a monotonic-derived stamp can flip a sign. It did:
    a tail of -0.017 ms, clamped and published as the `0.0` that means
    "measured, and instant".
    """

    @staticmethod
    def _steps():
        return [
            _step("THINKING", "DONE", thinking="plan", usage=_usage(100, 0, 5, 5)),
            _step(
                "TEXT_RESPONSE",
                "DONE",
                content="done",
                content_delta="done",
                complete=True,
                usage=_usage(200, 0, 10, 0),
            ),
        ]

    async def test_both_brackets_are_stamped_from_the_injected_clock(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("coder_eval.agent.TurnClock", AnchoredClock)
        seen: list[Any] = []
        await _agent_with_steps(self._steps()).communicate(
            "go", iteration=1, stream_callback=SimpleNamespace(on_event=seen.append)
        )

        assert_bracket_on_the_clock(seen)

    async def test_the_tail_is_a_measurement_rather_than_a_clamped_zero(self, monkeypatch: pytest.MonkeyPatch):
        """The published defect, asserted directly.

        `harness_teardown_ms` was `0.0` here because `decompose_turn` clamped a
        negative produced by two clock bases. The strict `> 0.0` that catches a
        revert lives in `assert_overhead_is_measured`, which every harness
        shares — this harness is simply where the margin is thinnest, since it
        holds its process across turns and so has the shortest real tail.
        """
        monkeypatch.setattr("coder_eval.agent.TurnClock", AnchoredClock)
        record = (await _agent_with_steps(self._steps()).communicate("go", iteration=1)).record

        assert_overhead_is_measured(record)


class TestCancellation:
    """A cancel from outside ends the turn first and propagates; a cancel the SDK raised itself is a crash."""

    async def test_an_external_cancel_ends_the_turn_once_and_propagates(self, tmp_path):
        started = asyncio.Event()

        class _HangingConversation(_FakeConversation):
            async def receive_steps(self):
                started.set()
                await asyncio.sleep(60)
                yield  # pragma: no cover - never reached

        agent = _agent_with_steps([])
        agent.working_directory = tmp_path
        agent._sdk_agent = SimpleNamespace(conversation=_HangingConversation([]), is_started=True)
        events: list[Any] = []
        callback = SimpleNamespace(on_event=events.append)
        task = asyncio.ensure_future(agent.communicate("go", iteration=1, stream_callback=callback, timeout=30))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ends = [e for e in events if isinstance(e, AgentEndEvent)]
        assert [(e.status, e.crash_reason) for e in ends] == [(AgentEndStatus.CRASHED, "turn cancelled")]
        assert agent.get_state() is AgentState.ERROR

    async def test_a_cancel_the_sdk_raised_itself_is_a_crashed_outcome(self, tmp_path):
        class _CancellingConversation(_FakeConversation):
            async def receive_steps(self):
                raise asyncio.CancelledError
                yield  # pragma: no cover - never reached

        agent = _agent_with_steps([])
        agent.working_directory = tmp_path
        agent._sdk_agent = SimpleNamespace(conversation=_CancellingConversation([]), is_started=True)
        outcome = await agent.communicate("go", iteration=1, timeout=30)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error == "Antigravity turn failed: the SDK was cancelled"

    async def test_a_failed_turn_keeps_its_generation_count_and_completed_reply(self, tmp_path):
        steps = [
            _step("THINKING", "DONE", thinking="plan", usage=_usage(10, 0, 1, 1)),
            _step("TEXT_RESPONSE", "DONE", content="partial answer", usage=_usage(10, 0, 2, 0)),
        ]

        class _ThenBoom(_FakeConversation):
            async def receive_steps(self):
                for step in steps:
                    yield step
                raise ValueError("stream died")

        agent = _agent_with_steps([])
        agent.working_directory = tmp_path
        agent._sdk_agent = SimpleNamespace(conversation=_ThenBoom([]), is_started=True)
        outcome = await agent.communicate("go", iteration=1, timeout=30)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.record.agent_output == "partial answer"
        assert outcome.record.assistant_turn_count == outcome.record.num_turns == 2
