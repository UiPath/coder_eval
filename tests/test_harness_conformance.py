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
from collections.abc import Awaitable, Callable
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
    Enforcement,
    FileExistsCriterion,
    HarnessContract,
    PermissionMode,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.harness_contract import HarnessContractError, validate_harness_contract
from coder_eval.plugins import ensure_plugins_loaded
from tests.test_antigravity_agent import _install_fake_sdk


MARKER = "CONFORMANCE-MARKER-7f3a"
USER_TURN = "do the task"
_FIELDS = ("system_prompt", "plugin_skills", "permission_mode", "allowed_tools", "disallowed_tools")
_CONFIG_FIELD = {"plugin_skills": "plugins"}
_KINDS = [kind for kind in AgentKind if kind is not AgentKind.UNKNOWN]

type Probe = Callable[[Path, pytest.MonkeyPatch], Awaitable[None]]


def _contract(kind: AgentKind) -> HarnessContract:
    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None
    return registration.agent_class.contract


def _task(kind: AgentKind, **agent: Any) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt=None if kind is AgentKind.NONE else USER_TURN,
        agent=parse_agent_config(type=kind, **agent),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
    )


def _plugin_root(tmp_path: Path) -> Path:
    skill = tmp_path / "plugin" / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: d\n---\n", encoding="utf-8")
    return tmp_path / "plugin"


def _plugins(tmp_path: Path) -> list[dict[str, str]]:
    return [{"type": "local", "path": str(_plugin_root(tmp_path))}]


_GATED_VALUES: dict[str, Any] = {
    "system_prompt": MARKER,
    "plugins": [{"type": "local", "path": "/plugins/p"}],
    "permission_mode": "plan",
    "allowed_tools": ["Bash"],
    "disallowed_tools": ["Bash"],
}


# --- rejections, derived from the contracts ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "field"),
    [(k, f) for k in _KINDS for f in _FIELDS if getattr(_contract(k), f) is Enforcement.UNSUPPORTED],
)
def test_unsupported_field_is_rejected(kind: AgentKind, field: str) -> None:
    config_field = _CONFIG_FIELD.get(field, field)
    with pytest.raises(HarnessContractError, match=rf"agent\.{config_field}.*{kind.value!r}"):
        validate_harness_contract(_task(kind, **{config_field: _GATED_VALUES[config_field]}))


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        (k, m)
        for k in _KINDS
        if _contract(k).permission_mode is Enforcement.ENFORCED
        for m in PermissionMode
        if m not in (_contract(k).permission_modes or frozenset())
    ],
)
def test_undeclared_permission_value_is_rejected(kind: AgentKind, mode: PermissionMode) -> None:
    with pytest.raises(HarnessContractError, match="has no documented meaning"):
        validate_harness_contract(_task(kind, permission_mode=mode))


@pytest.mark.parametrize(
    "kind", [k for k in _KINDS if AgentRegistry.get(k) and AgentRegistry.get(k).agent_class.tool_names]
)
def test_unknown_tool_name_is_rejected(kind: AgentKind) -> None:
    with pytest.raises(HarnessContractError, match="did you mean 'Bash'"):
        validate_harness_contract(_task(kind, allowed_tools=["Bassh"]))


# --- probes: the value reaches the native call --------------------------------------


async def _claude(tmp_path: Path, **agent: Any) -> tuple[Any, str]:
    captured: dict[str, Any] = {}

    async def fake_query(prompt: str, options: Any):
        captured["prompt"], captured["options"] = prompt, options
        yield type(
            "ResultMessage",
            (),
            {"session_id": "s", "usage": {}, "total_cost_usd": 0.0, "num_turns": 1, "is_error": False, "result": "ok"},
        )()

    claude = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, **agent))
    await claude.start(str(tmp_path))
    with patch("coder_eval.agents.claude_code_agent.query", fake_query):
        await claude.communicate(USER_TURN)
    return captured["options"], captured["prompt"]


async def _probe_claude_system_prompt(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    options, prompt = await _claude(tmp_path, system_prompt=MARKER)
    assert MARKER in str(options.system_prompt)
    assert MARKER not in prompt


async def _probe_claude_plugins(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    options, _ = await _claude(tmp_path, plugins=[{"type": "local", "path": str(root)}])
    assert [p["path"] for p in options.plugins] == [str(root)]


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
    await codex.communicate(USER_TURN)
    assert options["developer_instructions"] == MARKER
    assert turn_inputs == [USER_TURN]


async def _probe_codex_plugins(tmp_path: Path, _mp: pytest.MonkeyPatch) -> None:
    codex = CodexAgent(parse_agent_config(type=AgentKind.CODEX, plugins=_plugins(tmp_path)))
    codex.working_directory = tmp_path / "work"
    codex.working_directory.mkdir()
    codex._setup_skills(None)
    assert (codex.working_directory / ".agents" / "skills" / "probe-skill" / "SKILL.md").exists()


async def _antigravity_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **agent: Any) -> Any:
    configs: list[Any] = []

    class _FakeSdkAgent:
        def __init__(self, cfg: Any) -> None:
            configs.append(cfg)

        async def __aenter__(self) -> _FakeSdkAgent:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    _install_fake_sdk(monkeypatch, _FakeSdkAgent)
    await AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, **agent)).start(str(tmp_path))
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
    await agent.communicate(USER_TURN)
    assert sent == [USER_TURN]


async def _probe_antigravity_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    cfg = await _antigravity_config(tmp_path / "work", monkeypatch, plugins=[{"type": "local", "path": str(root)}])
    assert str(root / "skills") in cfg.skills_paths


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


async def _cli_agent(cls: type, kind: AgentKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **agent: Any):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    cli = cls(parse_agent_config(type=kind, model="provider/model", **agent), task_id="t1")
    await cli.start(str(tmp_path / "work"))
    return cli


async def _opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **agent: Any) -> tuple[dict[str, Any], list[str]]:
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    opencode = await _cli_agent(OpenCodeAgent, AgentKind.OPENCODE, tmp_path, monkeypatch, **agent)
    try:
        raw = opencode._build_env().get("OPENCODE_CONFIG_CONTENT")
        config = json.loads(raw) if raw else {}
        config["instructions_text"] = [Path(p).read_text(encoding="utf-8") for p in config.get("instructions", [])]
        return config, opencode._build_argv(USER_TURN)
    finally:
        await opencode.stop()


async def _probe_opencode_system_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, argv = await _opencode(tmp_path, monkeypatch, system_prompt=MARKER)
    assert config["instructions_text"] == [MARKER]
    assert MARKER not in " ".join(argv)


async def _probe_opencode_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin_root(tmp_path)
    config, _ = await _opencode(tmp_path, monkeypatch, plugins=[{"type": "local", "path": str(root)}])
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


async def _pi_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **agent: Any) -> list[str]:
    pi = await _cli_agent(PiAgent, AgentKind.PI, tmp_path, monkeypatch, **agent)
    try:
        return pi._build_argv(USER_TURN)
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
    argv = await _pi_argv(tmp_path, monkeypatch, plugins=[{"type": "local", "path": str(root)}])
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
    cells: set[tuple[str, str]] = set()
    for kind in _KINDS:
        contract = _contract(kind)
        for field in _FIELDS:
            if getattr(contract, field) is not Enforcement.ENFORCED:
                continue
            if field == "permission_mode":
                cells |= {(kind.value, f"permission_mode={m.value}") for m in contract.permission_modes or ()}
            else:
                cells.add((kind.value, field))
    return cells


def test_every_enforced_cell_has_exactly_one_probe() -> None:
    assert set(_PROBES) == _enforced_cells()


@pytest.mark.parametrize("cell", sorted(_PROBES))
async def test_probe(cell: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _PROBES[cell](tmp_path, monkeypatch)
