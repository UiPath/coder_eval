"""Tests for the Antigravity agent backend (registration, config, token/tool mapping).

These exercise the wiring and the pure mapping helpers; they do NOT require the
optional ``google-antigravity`` SDK (all SDK use is lazy, inside ``start()``).
"""

import asyncio
import logging
import os
import sys
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
        step_index=0,
    )


class _FakeConversation:
    def __init__(self, steps):
        self._steps = steps
        self.last_response = ""

    async def send(self, prompt, **kwargs):
        return None

    async def receive_steps(self):
        for s in self._steps:
            yield s

    async def cancel(self):
        return None


def _agent_with_steps(steps):
    from pathlib import Path

    agent = AntigravityAgent(parse_agent_config(type="antigravity", model="gemini-3.5-flash"))
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_FakeConversation(steps), is_started=True)
    return agent


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

    class _FiringWatchdog:
        def __init__(self, *, on_timeout, **_kwargs):
            self._on_timeout = on_timeout

        def __enter__(self):
            self._on_timeout()  # watchdog fired: state.timeout_hit = True
            return self

        def __exit__(self, *_exc):
            return False

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
        GeminiModelOptions=lambda **kw: SimpleNamespace(**kw),
        CapabilitiesConfig=lambda **kw: SimpleNamespace(**kw),
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
    await task_a

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


# --- allowed_tools / disallowed_tools / permission_mode -----------------------------
#
# These fields were ignored entirely before (policies were hardcoded to allow_all and
# no CapabilitiesConfig was built), so two harnesses reading the same task file ran
# different tasks. CapabilitiesConfig validates against the BuiltinTools enum, so the
# Claude→Antigravity name mapping has to be exact and unmappables must be dropped.


def _agent(**cfg) -> AntigravityAgent:
    return AntigravityAgent(parse_agent_config(type="antigravity", **cfg))


def _fake_types() -> SimpleNamespace:
    return SimpleNamespace(CapabilitiesConfig=lambda **kw: SimpleNamespace(**kw))


def test_claude_to_antigravity_tool_map_is_exact_inverse():
    """The inverse map is derived, so a forward-map edit can never leave it stale."""
    from coder_eval.agents.antigravity_agent import _CLAUDE_TO_ANTIGRAVITY_TOOL_MAP

    assert len(_CLAUDE_TO_ANTIGRAVITY_TOOL_MAP) == len(_ANTIGRAVITY_TO_CLAUDE_TOOL_MAP)
    for antigravity_name, claude_name in _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.items():
        assert _CLAUDE_TO_ANTIGRAVITY_TOOL_MAP[claude_name] == antigravity_name


def test_allowed_tools_become_enabled_tools():
    """The repo-default allowlist maps onto the matching Antigravity builtins."""
    agent = _agent(allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"])

    caps = agent._build_capabilities(_fake_types())

    assert caps.enabled_tools == [
        "run_command",
        "view_file",
        "create_file",
        "edit_file",
        "find_file",
        "search_directory",
        "finish",
    ]


def test_allowed_tools_always_keep_finish():
    """`finish` is how a turn ends — an allowlist must never strip it."""
    agent = _agent(allowed_tools=["Read"])

    caps = agent._build_capabilities(_fake_types())

    assert "finish" in caps.enabled_tools


def test_unmappable_allowed_tools_are_dropped_not_raised():
    """`Skill` has no Antigravity builtin (skills come from skills_paths) — drop it."""
    agent = _agent(allowed_tools=["Skill", "Bash"])

    caps = agent._build_capabilities(_fake_types())

    assert "Skill" not in caps.enabled_tools
    assert "run_command" in caps.enabled_tools


def test_allowlist_that_maps_to_nothing_falls_back_to_defaults(caplog):
    """An allowlist of only-unmappable names must not hand the model just `finish`.

    Enabling nothing but the turn-ender scores 0 with no diagnosable cause, so the
    harness default (all tools) plus a loud warning is the better failure mode.
    """
    agent = _agent(allowed_tools=["Skill", "TodoWrite"])

    with caplog.at_level(logging.WARNING, logger="coder_eval.agents.antigravity_agent"):
        caps = agent._build_capabilities(_fake_types())

    assert caps is None
    assert "maps to no usable Antigravity tool" in caplog.text


def test_disallowed_tools_are_subtracted_from_an_allowlist():
    """enabled_tools and disabled_tools are mutually exclusive in the SDK, so subtract."""
    agent = _agent(allowed_tools=["Bash", "Read", "Write"], disallowed_tools=["Write"])

    caps = agent._build_capabilities(_fake_types())

    # One field only — passing both would fail the SDK's mutual-exclusion validator.
    assert not hasattr(caps, "disabled_tools")
    assert caps.enabled_tools == ["run_command", "view_file", "finish"]


def test_disallowed_tools_alone_become_disabled_tools():
    agent = _agent(disallowed_tools=["WebSearch"])

    caps = agent._build_capabilities(_fake_types())

    assert caps.disabled_tools == ["search_web"]


def test_disallowed_tools_cannot_disable_a_structural_tool():
    agent = _agent(disallowed_tools=["Finish"])

    assert agent._build_capabilities(_fake_types()) is None


def test_no_tool_fields_leaves_harness_defaults():
    assert _agent()._build_capabilities(_fake_types()) is None


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions"])
async def test_permission_mode_never_confines_the_harness(monkeypatch, tmp_path, mode: str):
    """permission_mode is declared unhonored: every mode stays fully autonomous.

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


async def test_start_passes_capabilities_to_sdk_config(monkeypatch, tmp_path):
    """End-to-end: the allowlist reaches LocalAgentConfig, not just the builder."""
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    await _agent(allowed_tools=["Bash", "Read"]).start(str(tmp_path))

    assert configs[0].capabilities.enabled_tools == ["run_command", "view_file", "finish"]


async def test_start_omits_capabilities_when_unconstrained(monkeypatch, tmp_path):
    """No allowlist → the kwarg is absent entirely, so the SDK default stands."""
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)

    await _agent().start(str(tmp_path))

    assert not hasattr(configs[0], "capabilities")


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
