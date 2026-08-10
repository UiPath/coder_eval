"""Generic registry-agent worker protocol tests."""

from __future__ import annotations

import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from coder_eval.agent import Agent
from coder_eval.agents.registry import AgentRegistry
from coder_eval.isolation.agent_worker import _new_stop_path, _remove_stop_path, _WorkerServer
from coder_eval.models import AgentState, BaseAgentConfig, DirectRoute, TurnRecord
from coder_eval.streaming.callbacks import StreamCallback


class _PluginConfig(BaseAgentConfig):
    type: Literal["worker-test-plugin"] = "worker-test-plugin"  # type: ignore[assignment]


class _PluginAgent(Agent[_PluginConfig]):
    def __init__(self, config: _PluginConfig, route: DirectRoute | None = None, **kwargs: Any) -> None:
        self.config = config
        self.route = route
        self.kwargs = kwargs
        self._state = AgentState.WORKING
        self.started_as: tuple[int | None, int | None] | None = None

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        del working_directory, env_path_prepend, plugin_tools_dir
        import os

        self.started_as = (
            os.geteuid() if hasattr(os, "geteuid") else None,
            os.getegid() if hasattr(os, "getegid") else None,
        )

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        del stream_callback, timeout, max_turns, should_stop
        return TurnRecord(iteration=1, user_input=user_input, agent_output="plugin-ok")

    async def stop(self) -> None:
        self._mark_stopped()


class _EventWriter:
    def on_event(self, _event: object) -> None:
        return None


@pytest.mark.skipif(sys.platform != "linux", reason="the isolated worker runs only in Linux containers")
def test_stop_flag_lives_in_a_directory_the_agent_cannot_modify() -> None:
    stop_path = _new_stop_path()
    try:
        assert not stop_path.exists()
        assert stat.S_IMODE(stop_path.parent.stat().st_mode) == 0o711
    finally:
        _remove_stop_path(stop_path)


@pytest.fixture
def registered_worker_plugin():
    saved = dict(AgentRegistry._registry)
    AgentRegistry.register("worker-test-plugin", _PluginConfig)(_PluginAgent)
    try:
        yield
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(saved)


async def test_worker_constructs_any_registry_agent_without_an_allowlist(
    registered_worker_plugin: None, tmp_path: Path
) -> None:
    server = _WorkerServer()
    start_result, should_exit = await server.handle(
        "start",
        {
            "agent_kind": "worker-test-plugin",
            "config": {"type": "worker-test-plugin"},
            "route": {"type": "DirectRoute", "data": {"judge_transport": None}},
            "constructor_kwargs": {"marker": "forwarded"},
            "working_directory": str(tmp_path),
        },
    )

    assert should_exit is False
    assert start_result["state"] == AgentState.WORKING.value
    assert isinstance(server.agent, _PluginAgent)
    assert server.agent.kwargs == {"marker": "forwarded"}

    turn_result, should_exit = await server.handle(
        "communicate",
        {"user_input": "hello", "_writer": _EventWriter()},
    )

    assert should_exit is False
    assert TurnRecord.model_validate(turn_result["record"]).agent_output == "plugin-ok"
    await server.close()
