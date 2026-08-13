"""Tests for the Antigravity agent backend (registration, config, token/tool mapping).

These exercise the wiring and the pure mapping helpers; they do NOT require the
optional ``google-antigravity`` SDK (all SDK use is lazy, inside ``start()``).
"""

import asyncio
import os
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from coder_eval.agents.antigravity_agent import (
    _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP,
    _DEFAULT_MODEL,
    AntigravityAgent,
    _enum_value,
    _to_token_usage,
)
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import AgentKind, AntigravityAgentConfig, parse_agent_config
from coder_eval.plugins import ensure_plugins_loaded
from coder_eval.pricing import calculate_cost


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


def _make_skill(parent, name: str) -> None:
    d = parent / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n")


def test_resolve_skills_paths_prefers_nested_skills_layout(tmp_path):
    """A repo-root plugin source resolves to its ``skills/`` subdir (where SKILL.md live)."""
    repo = tmp_path / "skills-repo"
    _make_skill(repo / "skills", "uipath-troubleshoot")
    agent = AntigravityAgent(parse_agent_config(type="antigravity", plugins=[{"type": "local", "path": str(repo)}]))
    assert agent._resolve_skills_paths(None) == [str((repo / "skills").resolve())]


def test_resolve_skills_paths_accepts_direct_skills_dir_via_plugin_tools_dir(tmp_path):
    """A dir that *directly* parents skill dirs resolves to itself (no nested skills/)."""
    direct = tmp_path / "node_modules" / "@uipath"
    _make_skill(direct, "uipath-agents")
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    assert agent._resolve_skills_paths(str(direct)) == [str(direct.resolve())]


def test_resolve_skills_paths_dedupes_and_drops_unresolved(tmp_path):
    """Unresolved env-var paths are dropped; the same root from two sources appears once."""
    repo = tmp_path / "skills-repo"
    _make_skill(repo / "skills", "uipath-solution")
    agent = AntigravityAgent(
        parse_agent_config(
            type="antigravity",
            plugins=[
                {"type": "local", "path": "$DEFINITELY_UNSET_SKILLS_VAR/x"},
                {"type": "local", "path": str(repo)},
            ],
        )
    )
    # plugin_tools_dir points at the same resolved root as the plugin → deduped to one.
    assert agent._resolve_skills_paths(str(repo)) == [str((repo / "skills").resolve())]


def test_resolve_skills_paths_empty_without_sources():
    """No plugins and no plugin_tools_dir → empty list (harness default, no skills)."""
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    assert agent._resolve_skills_paths(None) == []


def test_resolve_workspaces_includes_workdir_and_skill_roots(tmp_path):
    """workspaces = sandbox workdir + resolved skill roots, so SKILL.md stays readable."""
    repo = tmp_path / "skills-repo"
    _make_skill(repo / "skills", "uipath-sdd")
    agent = AntigravityAgent(parse_agent_config(type="antigravity", plugins=[{"type": "local", "path": str(repo)}]))
    agent.working_directory = tmp_path / "work"
    skills_paths = agent._resolve_skills_paths(None)
    assert agent._resolve_workspaces(skills_paths) == [
        str(tmp_path / "work"),
        str((repo / "skills").resolve()),
    ]


def test_workspace_only_permits_skill_reads_with_resolved_workspaces(tmp_path):
    """Drives the harness's real ``workspace_only`` policy: a skill read is denied when
    scoped to the workdir alone (the bug), but permitted once the resolved skill roots
    join ``workspaces`` (the fix). ``skills_paths`` feeds discovery, not the file-tool
    allowlist, so the roots must be in ``workspaces`` for the agent to read SKILL.md."""
    policy = pytest.importorskip("google.antigravity.hooks.policy")
    ag_types = pytest.importorskip("google.antigravity.types")

    repo = tmp_path / "skills-repo"
    _make_skill(repo / "skills", "uipath-sdd")
    skill_md = repo / "skills" / "uipath-sdd" / "SKILL.md"
    workdir = tmp_path / "work"
    workdir.mkdir()

    agent = AntigravityAgent(parse_agent_config(type="antigravity", plugins=[{"type": "local", "path": str(repo)}]))
    agent.working_directory = workdir
    skills_paths = agent._resolve_skills_paths(None)

    def read_denied(workspaces) -> bool:
        policies = policy.workspace_only([str(w) for w in workspaces])
        tc = ag_types.ToolCall(name="read_file", canonical_path=str(skill_md))
        return any(p.when(tc) for p in policies if p.when is not None)

    assert read_denied([workdir]) is True  # pre-fix: out-of-workspace → denied
    assert read_denied(agent._resolve_workspaces(skills_paths)) is False  # fix permits it


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


def _usage(prompt: int, cached: int, candidates: int, thoughts: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        total_token_count=prompt + candidates + thoughts,
    )


def _tc(name: str, tid: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=tid, args=args)


def _step(
    stype,
    status,
    *,
    source="MODEL",
    target="TARGET_USER",
    tool_calls=None,
    content="",
    content_delta="",
    thinking="",
    thinking_delta="",
    usage=None,
    complete=None,
    error="",
    step_index=0,
    trajectory_id="",
):
    # Plain strings stand in for the SDK's str-enums (_enum_value passes them through).
    return SimpleNamespace(
        type=stype,
        status=status,
        source=source,
        target=target,
        tool_calls=tool_calls or [],
        content=content,
        content_delta=content_delta,
        thinking=thinking,
        thinking_delta=thinking_delta,
        usage_metadata=usage,
        is_complete_response=complete,
        error=error,
        step_index=step_index,
        trajectory_id=trajectory_id,
    )


class _FakeConversation:
    """Scriptable fake SDK conversation.

    ``steps`` is either a flat list (one batch, yielded on the first
    ``receive_steps()`` call) or a list of batches (one per successive
    ``receive_steps()`` call — the shape a poll loop drains repeatedly). Once
    the authored batches are exhausted, further calls yield an EMPTY batch —
    this mirrors the real SDK's local connection, which drains a queue and
    returns immediately with nothing once idle; it never replays already-
    yielded steps. A test standing in for a background job that never
    resolves should author one batch that opens the orphan and let
    exhaustion naturally fall through to empty polls, not repeat itself.
    """

    def __init__(self, steps):
        self._batches = list(steps) if steps and isinstance(steps[0], list) else [steps]
        self._batch_index = 0
        self.last_response = ""
        self.receive_steps_call_count = 0
        self.cancel_call_count = 0

    async def send(self, prompt, **kwargs):
        return None

    async def receive_steps(self):
        self.receive_steps_call_count += 1
        batch = self._batches[self._batch_index] if self._batch_index < len(self._batches) else []
        self._batch_index += 1
        for s in batch:
            yield s

    async def cancel(self):
        self.cancel_call_count += 1


def _agent_with_steps(steps):
    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity", model="gemini-3.5-flash"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_FakeConversation(steps), is_started=True)
    return agent


async def _no_sleep(_seconds: float) -> None:
    """Stand-in for asyncio.sleep in poll-loop tests — no real wait."""
    return None


class _FiringWatchdog:
    """Fake ThreadedWatchdog that fires ``on_timeout`` synchronously at entry.

    Sets ``state.timeout_hit = True`` before any draining happens (exactly
    like the real watchdog thread firing early), so a CancelledError raised
    later — whether from the first drain or from a poll loop's re-drain — is
    classified via the SAME existing ``if state.timeout_hit`` branch.
    """

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
    tr = await agent.communicate("make hello.py")

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
    assert agent.pending_turn is None  # success path leaves no partial


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
    tr = await _agent_with_steps(steps).communicate("x")
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
    tr = await _agent_with_steps(steps).communicate("run it")
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "error"


async def test_communicate_crash_sets_pending_partial_turn():
    """A mid-stream SDK error raises AgentCrashError and leaves a crashed partial."""
    from coder_eval.errors import AgentCrashError

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

    with pytest.raises(AgentCrashError):
        await agent.communicate("x")
    assert agent.pending_turn is not None
    assert agent.pending_turn.crashed is True

    await agent.discard_pending_turn()
    assert agent.pending_turn is None


async def test_communicate_timeout_sets_pending_partial_turn(monkeypatch):
    """A turn timeout raises TurnTimeoutError and leaves a crashed partial turn.

    Drives the timeout branch deterministically: a fake watchdog fires its
    ``on_timeout`` callback synchronously on entry (setting ``state.timeout_hit``,
    exactly what the real watchdog thread does), and the step pump then surfaces
    the cancel as ``asyncio.CancelledError`` — the :402-404 timeout branch.
    """
    import asyncio

    from coder_eval.errors import TurnTimeoutError

    monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _FiringWatchdog)

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

    with pytest.raises(TurnTimeoutError):
        await agent.communicate("x", timeout=30.0)
    assert agent.pending_turn is not None
    assert agent.pending_turn.crashed is True

    await agent.discard_pending_turn()
    assert agent.pending_turn is None


async def test_communicate_requires_started_agent():
    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    with pytest.raises(RuntimeError, match="not started"):
        await agent.communicate("x")


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
    from coder_eval.agents.antigravity_agent import _AntigravityTurnState

    state = _AntigravityTurnState.__new__(_AntigravityTurnState)
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
    tr = await agent.communicate("run it")

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
    tr = await agent.communicate("do it")

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
    tr = await agent.communicate("do it")

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
    tr = await agent.communicate("do it")

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
    tr = await agent.communicate("do two things")

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
    tr = await agent.communicate("do two things")

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
    tr = await agent.communicate("do it forever")

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
    # driven by the deadline, not by exhausting all 120 cycles.
    clock = iter([0.0, 130.0, 260.0])
    monkeypatch.setattr(antigravity_agent.time, "monotonic", lambda: next(clock, 1_000_000.0))

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

    tr = await agent.communicate("do it forever", timeout=300.0)  # the real default turn_timeout

    # Finalized and graded -- no TurnTimeoutError, no crash.
    assert tr is not None
    assert not tr.crashed
    bash = next(c for c in tr.commands if c.tool_name == "Bash")
    assert bash.result_status == "unknown"  # force-closed as UNRESOLVED by finalize()
    # Exited via the poll_deadline (well under the 120-cycle cap), matching a
    # real turn where the watchdog's 300s cutoff never gets the chance to fire.
    assert agent._sdk_agent.conversation.receive_steps_call_count < 5


class _WatchdogFiresLater:
    """Fake ThreadedWatchdog that does NOT fire on entry (unlike _FiringWatchdog
    above) -- it hands its ``on_timeout`` callback to the caller so the test can
    invoke it mid-poll-loop, simulating a real watchdog thread firing between
    poll cycles rather than before the turn even starts."""

    captured_on_timeout: Callable[[], None] | None = None

    def __init__(self, *, on_timeout, **_kwargs):
        _WatchdogFiresLater.captured_on_timeout = on_timeout

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


async def test_communicate_poll_loop_exits_promptly_once_watchdog_flag_lands(monkeypatch):
    """A watchdog timeout landing BETWEEN poll cycles (state.timeout_hit flips
    to True while the loop is sleeping) must stop the loop on its next condition
    check, not burn through the rest of _MAX_BACKGROUND_POLLS waiting for a
    cancellation that may not land on this coroutine right away (final-review
    finding: the loop condition must read the flag the watchdog already set)."""
    from coder_eval.agents import antigravity_agent

    monkeypatch.setattr(antigravity_agent, "_MAX_BACKGROUND_POLLS", 50)
    monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _WatchdogFiresLater)

    sleep_calls: list[float] = []

    async def _fire_watchdog_on_second_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            assert _WatchdogFiresLater.captured_on_timeout is not None
            _WatchdogFiresLater.captured_on_timeout()

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
    from coder_eval.errors import TurnTimeoutError

    agent = _agent_with_steps([never_closing])
    with pytest.raises(TurnTimeoutError):
        await agent.communicate("do it forever", timeout=30.0)

    # Stopped right after the sleep that flipped timeout_hit -- NOT the (patched) cap of 50.
    assert len(sleep_calls) == 2
    # 1 initial drain + 1 poll re-drain (after sleep #1) -- the mid-loop
    # `if state.timeout_hit: break` skips the re-drain that would otherwise
    # follow sleep #2, so no 3rd receive_steps() call happens.
    assert agent._sdk_agent.conversation.receive_steps_call_count == 2
    assert agent.pending_turn is not None
    bash = next(c for c in agent.pending_turn.commands if c.tool_name == "Bash")
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

    def should_stop() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2  # False for batch1's 2 steps; True on the post-sleep check

    await agent.communicate("do it", should_stop=should_stop)

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
    AgentCrashError."""
    from pathlib import Path

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

    await agent.communicate("do it", should_stop=lambda: True)  # breaks after the first step

    # Without the retry, this second call raises AgentCrashError wrapping the
    # fake's RuntimeError (verified live before the fix landed). With it, the
    # transient window clears within a couple of asyncio.sleep(0) yields and
    # the second turn's real content is delivered, not silently dropped.
    tr = await agent.communicate("do it again")
    assert tr.agent_output == "second turn"


async def test_communicate_poll_budget_exhausted_finalizes_via_existing_timeout_path(monkeypatch):
    """A watchdog timeout landing during the poll loop's re-drain (not the first
    drain) must surface as TurnTimeoutError via the SAME existing exception
    branch -- the poll loop must not create a second, inconsistent timeout path.

    Uses ``_WatchdogFiresLater`` (not ``_FiringWatchdog``, which fires at entry
    and would make the loop's head condition skip the poll cycle entirely, per
    round-3 review) so ``state.timeout_hit`` only flips once a re-drain is
    genuinely in flight -- mirroring the real watchdog, whose ``on_timeout``
    callback and the ``CancelledError`` it triggers are the same causal event,
    not two independently-timed ones."""
    from coder_eval.agents import antigravity_agent
    from coder_eval.errors import TurnTimeoutError

    monkeypatch.setattr(antigravity_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _WatchdogFiresLater)

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
                assert _WatchdogFiresLater.captured_on_timeout is not None
                _WatchdogFiresLater.captured_on_timeout()
                raise asyncio.CancelledError
                yield  # pragma: no cover - makes this an async generator

        async def cancel(self):
            return None

    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity"))
    agent.working_directory = Path("/tmp")
    conversation = _FiresWatchdogThenCancelsOnSecondDrain()
    agent._sdk_agent = SimpleNamespace(conversation=conversation, is_started=True)

    with pytest.raises(TurnTimeoutError):
        await agent.communicate("x", timeout=30.0)
    assert conversation.call_count == 2  # the re-drain genuinely ran, not skipped
    assert agent.pending_turn is not None
    assert agent.pending_turn.crashed is True

    await agent.discard_pending_turn()
    assert agent.pending_turn is None


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


# --- permission_mode ----------------------------------------------------------------
#
# The local harness has one mode: policies are hardcoded to allow_all, so no
# permission_mode confines it. These pin that as intended behavior rather than an
# oversight — the write boundary is the sandbox driver, and a headless eval has
# nobody to approve anything.


def _agent(**cfg) -> AntigravityAgent:
    return AntigravityAgent(parse_agent_config(type="antigravity", **cfg))


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions"])
async def test_permission_mode_never_confines_the_harness(monkeypatch, tmp_path, mode: str):
    """permission_mode is not honored here: every mode stays fully autonomous.

    coder_eval's write boundary is the driver (docker container / ephemeral tempdir),
    not the agent — same deliberate stance as Codex. A mode that silently switched the
    policy list would make an A/B across harnesses incomparable.
    """
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


# --- max_turns visible-turn cap -----------------------------------------------------
#
# max_turns was accepted and never read on this backend, so a task capping turns ran
# uncapped here while the same file capped on Claude Code. The cap counts VISIBLE
# turns (tool calls — reports_stats.visible_turn_count's unit), enforced on the same
# step-loop boundary as the cooperative stop.


def _tool_steps(count: int) -> list:
    """`count` complete tool calls, each an ACTIVE step followed by its DONE step."""
    steps = []
    for i in range(count):
        call = _tc("run_command", f"t{i}", {"command_line": f"echo {i}"})
        steps.append(_step("TOOL_CALL", "ACTIVE", target="TARGET_ENVIRONMENT", tool_calls=[call]))
        done = _tc("run_command", f"t{i}", {"command_line": f"echo {i}", "exit_code": 0, "combined_output": str(i)})
        steps.append(_step("TOOL_CALL", "DONE", target="TARGET_ENVIRONMENT", tool_calls=[done]))
    return steps


async def test_max_turns_caps_visible_turns():
    """The stream offers 5 tool calls; max_turns=2 keeps 2 and never pulls the rest."""
    agent = _agent_with_steps(_tool_steps(5))

    record = await agent.communicate("go", max_turns=2)

    assert len(record.commands) == 2
    assert record.max_turns_exhausted is True


async def test_max_turns_keeps_the_deciding_step_whole():
    """The tool call that reaches the cap is completed, not cut mid-flight."""
    agent = _agent_with_steps(_tool_steps(3))

    record = await agent.communicate("go", max_turns=1)

    assert len(record.commands) == 1
    assert record.commands[0].result_status == "success"
    assert record.commands[0].result_summary == "0"


async def test_under_the_cap_completes_normally():
    agent = _agent_with_steps(_tool_steps(2))

    record = await agent.communicate("go", max_turns=5)

    assert len(record.commands) == 2
    assert record.max_turns_exhausted is False


async def test_no_max_turns_is_uncapped():
    """None must preserve the pre-existing behavior exactly."""
    agent = _agent_with_steps(_tool_steps(4))

    record = await agent.communicate("go")

    assert len(record.commands) == 4
    assert record.max_turns_exhausted is False


async def test_cooperative_stop_outranks_the_cap():
    """Both firing on the same step reports STOPPED_EARLY — the more specific reason."""
    agent = _agent_with_steps(_tool_steps(5))

    record = await agent.communicate("go", max_turns=1, should_stop=lambda: True)

    assert record.max_turns_exhausted is False
    assert len(record.commands) == 1


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
    # conditions are live at once, and the cap has to win: otherwise the loop keeps
    # polling out a background job on a run that is already over.
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

    record = await agent.communicate("go", max_turns=2)

    assert record.max_turns_exhausted is True
    # The cap counts RESOLVED calls. The still-open bg2 is force-closed and recorded
    # as unresolved rather than dropped, so the trajectory shows what was interrupted.
    resolved = [c for c in record.commands if c.result_status != "unknown"]
    assert [c.tool_id for c in resolved] == ["bg1", "t0"]
    assert [c.tool_id for c in record.commands if c.result_status == "unknown"] == ["bg2"]
    assert conv.receive_steps_call_count == 2  # initial drain + one poll re-drain, then stop
    assert conv.cancel_call_count == 1
