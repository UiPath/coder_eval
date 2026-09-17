"""Every in-tree harness honors what its contract claims, and rejects what it does not.

The agent class is the single source of truth: rejection cases are derived from each
``HarnessContract``, and every ENFORCED cell (every declared ``permission_modes``
value, for ``permission_mode``) must have a probe below that proves the value reaches
the native call, offline. A ``system_prompt`` probe also proves the user turn reaches
the harness unchanged, so an adapter that prefixes the user message cannot claim
``append``. Each tool-list probe includes a name the harness has no tool for.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agents.opencode_agent import OpenCodeAgent
from coder_eval.agents.pi_agent import PiAgent
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import (
    AgentKind,
    HarnessContract,
    PermissionMode,
    parse_agent_config,
)
from coder_eval.orchestration.plugin_staging import stage_plugins
from coder_eval.plugins import ensure_plugins_loaded
from coder_eval.streaming.events import StopReason
from coder_eval.testing import (
    FIRST_TOOL_ID,
    SECOND_TOOL_ID,
    StopAfterFirstTool,
    conformance,
    enforced_cells,
    rejections,
    stop_conformance,
)
from tests.test_antigravity_agent import _install_fake_sdk


MARKER = "CONFORMANCE-MARKER-7f3a"
USER_TURN = "do the task"
_KINDS = [kind for kind in AgentKind if kind is not AgentKind.UNKNOWN]

type Probe = Callable[[Path, pytest.MonkeyPatch], Awaitable[None]]


def _contract(kind: AgentKind) -> HarnessContract:
    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None
    return registration.agent_class.contract


def _plugin_root(tmp_path: Path) -> Path:
    """A root staged by ``stage_plugins`` over one authored ``probe-skill``."""
    skill = tmp_path / "plugin" / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: d\n---\n", encoding="utf-8")
    return stage_plugins([{"type": "local", "path": str(tmp_path / "plugin")}], tmp_path / "plugin_root").root


# --- rejections, derived from the contracts ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "check"),
    [pytest.param(k, check, id=f"{k.value}-{name}") for k in _KINDS for name, check in rejections(k.value)],
)
def test_contract_rejection(kind: AgentKind, check: Callable[[], None]) -> None:
    check()


def test_every_kind_rejects_what_its_contract_does_not_honor() -> None:
    names = {name for k in _KINDS for name, _ in rejections(k.value)}
    assert {
        "unsupported system_prompt",
        "undeclared permission_mode=default",
        "misspelled tool name",
        "unsupported run_limits.max_turns",
        "unsupported run_limits.expected_turns",
    } <= names


# --- probes: the value reaches the native call --------------------------------------


async def _claude(tmp_path: Path, plugin_root: Path | None = None, **agent: Any) -> tuple[Any, str]:
    captured: dict[str, Any] = {}

    async def fake_query(prompt: str, options: Any):
        captured["prompt"], captured["options"] = prompt, options
        yield type(
            "ResultMessage",
            (),
            {"session_id": "s", "usage": {}, "total_cost_usd": 0.0, "num_turns": 1, "is_error": False, "result": "ok"},
        )()

    claude = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, **agent))
    await claude.start(str(tmp_path), plugin_root=plugin_root)
    with patch("coder_eval.agents.claude_code_agent.query", fake_query):
        await claude.communicate(USER_TURN, iteration=1)
    return captured["options"], captured["prompt"]


async def _probe_claude_system_prompt(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    options, prompt = await _claude(tmp_path, system_prompt=MARKER)
    assert MARKER in str(options.system_prompt)
    assert MARKER not in prompt


async def _probe_claude_plugins(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    options, _ = await _claude(tmp_path, plugin_root=root)
    assert options.plugins == [{"type": "local", "path": str(root / "plugins" / "plugin")}]


async def test_claude_loads_no_plugin_without_a_plugin_root(tmp_path: Path) -> None:
    options, _ = await _claude(tmp_path)
    assert options.plugins == []


def _claude_mode(mode: PermissionMode) -> Probe:
    async def probe(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
        options, _ = await _claude(tmp_path, permission_mode=mode)
        assert options.permission_mode == mode.value

    return probe


async def _probe_claude_allowed(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    options, _ = await _claude(tmp_path, allowed_tools=["Bash"])
    assert options.allowed_tools == ["Bash"]


async def _probe_claude_disallowed(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    options, _ = await _claude(tmp_path, disallowed_tools=["Bash"])
    assert "Bash" in options.disallowed_tools


async def _probe_codex_system_prompt(_tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    from tests.test_codex_agent import _FakeThread, _started_agent, _turn_completed

    turn_inputs: list[str] = []

    class _RecordingThread(_FakeThread):
        def turn(self, user_input: str):  # type: ignore[override]
            turn_inputs.append(user_input)
            return super().turn(user_input)

    codex = _started_agent(parse_agent_config(type=AgentKind.CODEX, system_prompt=MARKER), [_turn_completed()])
    options = codex._build_thread_options()
    codex.thread = _RecordingThread([_turn_completed()])
    await codex.communicate(USER_TURN, iteration=1)
    assert options["developer_instructions"] == MARKER
    assert turn_inputs == [USER_TURN]


async def _probe_codex_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import openai_codex

    root = _plugin_root(tmp_path)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(openai_codex, "Codex", lambda **_kw: SimpleNamespace(close=lambda: None))
    work = tmp_path / "work"
    work.mkdir()
    await CodexAgent(parse_agent_config(type=AgentKind.CODEX)).start(str(work), plugin_root=root)
    linked = work / ".agents" / "skills" / "probe-skill"
    assert linked.resolve() == (root / "skills" / "probe-skill").resolve()


async def _antigravity_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_root: Path | None = None, **agent: Any
) -> Any:
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg: Any) -> None:
            configs.append(cfg)

        async def __aenter__(self) -> _FakeSdkAgent:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)
    await AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, **agent)).start(
        str(tmp_path), plugin_root=plugin_root
    )
    return configs[0]


def _policy_pairs(cfg: Any) -> list[tuple[str, str | None]]:
    return [(p.kind, getattr(p, "tool", None)) for p in cfg.policies]


async def _probe_antigravity_system_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._fixtures.golden_streams.antigravity_fixtures import _FakeConversation, _step, _usage

    cfg = await _antigravity_config(tmp_path, monkeypatch, system_prompt=MARKER)
    assert cfg.system_instructions == MARKER

    sent: list[str] = []

    class _RecordingConversation(_FakeConversation):
        async def send(self, prompt: str, **kwargs: Any) -> None:
            sent.append(prompt)

    done = _step("TEXT_RESPONSE", "DONE", content="ok", complete=True, usage=_usage(10, 0, 1, 0))
    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, system_prompt=MARKER))
    agent.working_directory = tmp_path
    agent._sdk_agent = SimpleNamespace(conversation=_RecordingConversation([done]), is_started=True)
    await agent.communicate(USER_TURN, iteration=1)
    assert sent == [USER_TURN]


async def _probe_antigravity_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    cfg = await _antigravity_config(tmp_path / "work", monkeypatch, plugin_root=root)
    assert cfg.skills_paths == [str(root / "skills")]


async def _probe_antigravity_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = await _antigravity_config(tmp_path, monkeypatch, permission_mode="plan")
    assert {("deny", t) for t in ("create_file", "edit_file", "run_command")} <= set(_policy_pairs(cfg))


async def _probe_antigravity_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = await _antigravity_config(tmp_path, monkeypatch, permission_mode="bypassPermissions")
    assert _policy_pairs(cfg) == [("allow_all", None)]


async def _probe_antigravity_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = await _antigravity_config(tmp_path, monkeypatch, allowed_tools=["Bash", "NotebookEdit"])
    assert _policy_pairs(cfg) == [("deny_all", None), ("allow", "finish"), ("allow", "run_command")]


async def _probe_antigravity_disallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = await _antigravity_config(tmp_path, monkeypatch, disallowed_tools=["Bash", "NotebookEdit"])
    assert _policy_pairs(cfg) == [("allow_all", None), ("deny", "run_command")]


async def _cli_agent(
    cls: type,
    kind: AgentKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_root: Path | None = None,
    **agent: Any,
):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    cli = cls(parse_agent_config(type=kind, model="provider/model", **agent), task_id="t1")
    await cli.start(str(tmp_path / "work"), plugin_root=plugin_root)
    return cli


async def _opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_root: Path | None = None, **agent: Any
) -> tuple[dict[str, Any], list[str]]:
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    opencode = await _cli_agent(OpenCodeAgent, AgentKind.OPENCODE, tmp_path, monkeypatch, plugin_root, **agent)
    try:
        raw = opencode.env().get("OPENCODE_CONFIG_CONTENT")
        config = json.loads(raw) if raw else {}
        config["instructions_text"] = [Path(p).read_text(encoding="utf-8") for p in config.get("instructions", [])]
        return config, opencode.argv(USER_TURN)
    finally:
        await opencode.stop()


async def _probe_opencode_system_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, argv = await _opencode(tmp_path, monkeypatch, system_prompt=MARKER)
    assert config["instructions_text"] == [MARKER]
    assert MARKER not in " ".join(argv)


async def _probe_opencode_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    config, _ = await _opencode(tmp_path, monkeypatch, plugin_root=root)
    assert config["skills"]["paths"] == [str(root / "skills")]


async def _probe_opencode_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, argv = await _opencode(tmp_path, monkeypatch, permission_mode="plan")
    assert config["permission"] == {"edit": "deny", "bash": "deny"}
    assert "--auto" in argv


async def _probe_opencode_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, argv = await _opencode(tmp_path, monkeypatch, permission_mode="bypassPermissions")
    assert "permission" not in config
    assert "--auto" in argv


async def _probe_opencode_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = await _opencode(tmp_path, monkeypatch, allowed_tools=["Bash", "NotebookEdit"])
    assert config["permission"] == {"*": "deny", "external_directory": "allow", "doom_loop": "allow", "bash": "allow"}


async def _probe_opencode_disallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = await _opencode(tmp_path, monkeypatch, disallowed_tools=["Bash", "NotebookEdit"])
    assert config["permission"] == {"bash": "deny"}


async def _pi_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_root: Path | None = None, **agent: Any
) -> list[str]:
    pi = await _cli_agent(PiAgent, AgentKind.PI, tmp_path, monkeypatch, plugin_root, **agent)
    try:
        return pi.argv(USER_TURN)
    finally:
        await pi.stop()


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


async def _probe_pi_system_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = await _pi_argv(tmp_path, monkeypatch, system_prompt=MARKER)
    assert _flag(argv, "--append-system-prompt") == MARKER
    assert MARKER not in argv[argv.index("--") + 1 :]


async def _probe_pi_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    argv = await _pi_argv(tmp_path, monkeypatch, plugin_root=root)
    assert _flag(argv, "--skill") == str(root / "skills")


async def _probe_pi_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = await _pi_argv(tmp_path, monkeypatch, permission_mode="plan")
    assert _flag(argv, "--exclude-tools") == "bash,edit,multiedit,patch,write"


async def _probe_pi_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = await _pi_argv(tmp_path, monkeypatch, permission_mode="bypassPermissions")
    assert "--tools" not in argv and "--exclude-tools" not in argv


async def _probe_pi_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _flag(await _pi_argv(tmp_path, monkeypatch, allowed_tools=["Bash", "Skill"]), "--tools") == "bash"


async def _probe_pi_disallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = await _pi_argv(tmp_path, monkeypatch, disallowed_tools=["Bash", "Skill"])
    assert _flag(argv, "--exclude-tools") == "bash"


_PROBES: dict[tuple[str, str], Probe] = {
    ("claude-code", "system_prompt"): _probe_claude_system_prompt,
    ("claude-code", "plugin_skills"): _probe_claude_plugins,
    **{("claude-code", f"permission_mode={m.value}"): _claude_mode(m) for m in PermissionMode},
    ("claude-code", "allowed_tools"): _probe_claude_allowed,
    ("claude-code", "disallowed_tools"): _probe_claude_disallowed,
    ("codex", "system_prompt"): _probe_codex_system_prompt,
    ("codex", "plugin_skills"): _probe_codex_plugins,
    ("antigravity", "system_prompt"): _probe_antigravity_system_prompt,
    ("antigravity", "plugin_skills"): _probe_antigravity_plugins,
    ("antigravity", "permission_mode=plan"): _probe_antigravity_plan,
    ("antigravity", "permission_mode=bypassPermissions"): _probe_antigravity_bypass,
    ("antigravity", "allowed_tools"): _probe_antigravity_allowed,
    ("antigravity", "disallowed_tools"): _probe_antigravity_disallowed,
    ("opencode", "system_prompt"): _probe_opencode_system_prompt,
    ("opencode", "plugin_skills"): _probe_opencode_plugins,
    ("opencode", "permission_mode=plan"): _probe_opencode_plan,
    ("opencode", "permission_mode=bypassPermissions"): _probe_opencode_bypass,
    ("opencode", "allowed_tools"): _probe_opencode_allowed,
    ("opencode", "disallowed_tools"): _probe_opencode_disallowed,
    ("pi", "system_prompt"): _probe_pi_system_prompt,
    ("pi", "plugin_skills"): _probe_pi_plugins,
    ("pi", "permission_mode=plan"): _probe_pi_plan,
    ("pi", "permission_mode=bypassPermissions"): _probe_pi_bypass,
    ("pi", "allowed_tools"): _probe_pi_allowed,
    ("pi", "disallowed_tools"): _probe_pi_disallowed,
}


def _enforced_cells() -> set[tuple[str, str]]:
    return {cell for kind in _KINDS for cell in enforced_cells(_contract(kind), kind.value)}


def test_every_enforced_cell_has_exactly_one_probe() -> None:
    assert set(_PROBES) == _enforced_cells()


@pytest.mark.parametrize("cell", sorted(_PROBES))
async def test_probe(cell: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _PROBES[cell](tmp_path, monkeypatch)


@pytest.mark.parametrize("kind", _KINDS, ids=lambda k: k.value)
async def test_conformance_per_kind(kind: AgentKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def bound(cell: tuple[str, str], probe: Probe) -> Callable[[], Awaitable[None]]:
        directory = tmp_path / cell[1].replace("=", "-")
        directory.mkdir()
        return lambda: probe(directory, monkeypatch)

    await conformance(
        kind.value, {cell: bound(cell, probe) for cell, probe in _PROBES.items() if cell[0] == kind.value}
    )


# --- cooperative stop: every StopReason ends the turn at the boundary ---------------


type StopProbe = Callable[[Path, pytest.MonkeyPatch, StopAfterFirstTool], Awaitable[list[Any]]]


def _recording(items: list[Any], pulled: list[Any]) -> Iterator[Any]:
    for item in items:
        pulled.append(item)
        yield item


async def _stop_claude(tmp_path: Path, _mp: pytest.MonkeyPatch, stop: StopAfterFirstTool) -> list[Any]:
    from tests._fixtures.golden_streams.claude_fixtures import (
        AssistantMessage,
        ResultMessage,
        ToolUseBlock,
        UserMessage,
    )

    pulled: list[Any] = []
    events = [
        AssistantMessage([ToolUseBlock(FIRST_TOOL_ID, "Bash", {"command": "ls"})], message_id="m1"),
        UserMessage(FIRST_TOOL_ID, False, "ok"),
        AssistantMessage([ToolUseBlock(SECOND_TOOL_ID, "Bash", {"command": "ls"})], message_id="m2"),
        UserMessage(SECOND_TOOL_ID, False, "ok"),
        ResultMessage(),
    ]

    async def fake_query(prompt: Any, options: Any, transport: Any = None):
        for event in _recording(events, pulled):
            yield event

    claude = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))
    await claude.start(str(tmp_path))
    with patch("coder_eval.agents.claude_code_agent.query", fake_query):
        await claude.communicate(USER_TURN, iteration=1, stream_callback=stop, should_stop=stop)
    return [getattr(e.content[0], "id", None) for e in pulled if hasattr(e, "content")]


async def _stop_codex(_tmp: Path, _mp: pytest.MonkeyPatch, stop: StopAfterFirstTool) -> list[Any]:
    from tests.test_codex_agent import _FakeThread, _FakeTurnHandle, _item_notification, _started_agent

    pulled: list[Any] = []

    def command(item_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            type="commandExecution", id=item_id, command="ls", exit_code=0, aggregated_output="ok", duration_ms=1
        )

    notifications = [
        _item_notification(method, command(item_id))
        for item_id in (FIRST_TOOL_ID, SECOND_TOOL_ID)
        for method in ("item/started", "item/completed")
    ]

    class _RecordingHandle(_FakeTurnHandle):
        def stream(self):  # type: ignore[override]
            return _recording(notifications, pulled)

    class _RecordingThread(_FakeThread):
        def turn(self, _user_input: str):  # type: ignore[override]
            self.last_handle = _RecordingHandle(notifications)
            return self.last_handle

    codex = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
    codex.thread = _RecordingThread(notifications)
    await codex.communicate(USER_TURN, iteration=1, stream_callback=stop, should_stop=stop)
    return [n.payload.item.root.id for n in pulled]


async def _stop_antigravity(tmp_path: Path, _mp: pytest.MonkeyPatch, stop: StopAfterFirstTool) -> list[Any]:
    from tests._fixtures.golden_streams.antigravity_fixtures import _FakeConversation, _step, _tc

    pulled: list[Any] = []

    def call(tool_id: str) -> list[Any]:
        args = {"command_line": "ls"}
        return [
            _step("TOOL_CALL", "ACTIVE", target="TARGET_ENVIRONMENT", tool_calls=[_tc("run_command", tool_id, args)]),
            _step(
                "TOOL_CALL",
                "DONE",
                target="TARGET_ENVIRONMENT",
                tool_calls=[_tc("run_command", tool_id, {**args, "exit_code": 0, "combined_output": "ok"})],
            ),
        ]

    steps = [*call(FIRST_TOOL_ID), *call(SECOND_TOOL_ID)]

    class _RecordingConversation(_FakeConversation):
        async def receive_steps(self):
            self.receive_steps_call_count += 1
            for step in _recording(steps if self.receive_steps_call_count == 1 else [], pulled):
                yield step

    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY))
    agent.working_directory = tmp_path
    agent._sdk_agent = SimpleNamespace(conversation=_RecordingConversation([]), is_started=True)
    await agent.communicate(USER_TURN, iteration=1, stream_callback=stop, should_stop=stop)
    return [s.tool_calls[0].id for s in pulled]


async def _stop_cli(
    cls: type, kind: AgentKind, lines: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: Any
) -> list[Any]:
    from tests._fixtures.golden_streams.pi_fixtures import _FakeProcess

    pulled: list[Any] = []

    class _RecordingProcess(_FakeProcess):
        async def readline(self) -> bytes:
            line = await super().readline()
            if line:
                pulled.append(json.loads(line))
            return line

    proc = _RecordingProcess(lines)

    async def fake_exec(*_argv: str, **_kwargs: Any) -> _RecordingProcess:
        proc.stderr = proc  # type: ignore[assignment]
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("os.killpg", lambda _pgid, _sig: None, raising=False)
    cli = await _cli_agent(cls, kind, tmp_path, monkeypatch)
    try:
        await cli.communicate(USER_TURN, iteration=1, stream_callback=stop, should_stop=stop)
    finally:
        await cli.stop()
    return [tool_id for tool_id in (FIRST_TOOL_ID, SECOND_TOOL_ID) if any(tool_id in json.dumps(p) for p in pulled)]


async def _stop_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: StopAfterFirstTool) -> list[Any]:
    from tests._fixtures.golden_streams.pi_fixtures import _tool_end, _tool_start, _turn_end, _turn_start

    lines = [_turn_start()]
    for tool_id in (FIRST_TOOL_ID, SECOND_TOOL_ID):
        lines += [_tool_start(tool_id, "bash", {"command": "ls"}), _tool_end(tool_id, "bash", "ok")]
    lines.append(_turn_end(inp=1, out=1))
    return await _stop_cli(PiAgent, AgentKind.PI, lines, tmp_path, monkeypatch, stop)


async def _stop_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: StopAfterFirstTool) -> list[Any]:
    from tests._fixtures.golden_streams.opencode_fixtures import _evt

    def tool_use(tool_id: str) -> str:
        state = {"status": "completed", "input": {"command": "ls"}, "output": "ok"}
        return _evt(
            "tool_use",
            {
                "id": f"prt_{tool_id}",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "bash",
                "callID": tool_id,
                "state": state,
            },
        )

    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    lines = [
        _evt("step_start", {"id": "prt_0", "messageID": "msg_1", "type": "step-start"}),
        tool_use(FIRST_TOOL_ID),
        tool_use(SECOND_TOOL_ID),
    ]
    return await _stop_cli(OpenCodeAgent, AgentKind.OPENCODE, lines, tmp_path, monkeypatch, stop)


_STOP_PROBES: dict[str, StopProbe] = {
    "claude-code": _stop_claude,
    "codex": _stop_codex,
    "antigravity": _stop_antigravity,
    "pi": _stop_pi,
    "opencode": _stop_opencode,
}


def test_every_cooperative_kind_has_a_stop_probe() -> None:
    assert set(_STOP_PROBES) == {k.value for k in _KINDS if _contract(k).cooperative_stop}


@pytest.mark.parametrize("kind", sorted(_STOP_PROBES))
async def test_stop_conformance(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(stop: StopAfterFirstTool, reason: StopReason) -> list[Any]:
        directory = tmp_path / reason.value
        directory.mkdir()
        return await _STOP_PROBES[kind](directory, monkeypatch, stop)

    await stop_conformance(kind, probe)
