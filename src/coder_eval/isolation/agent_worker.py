"""Unprivileged process boundary for evaluated agents.

The in-container orchestrator keeps ownership of trusted sandbox preparation and
grading.  When UID/GID isolation is enabled it talks to exactly one stateful
worker process which constructs and drives the registry-selected ``Agent`` as
the dedicated ``agent`` user.  This makes isolation independent of SDK-specific
subprocess hooks and therefore applies to third-party AgentRegistry plugins too.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import secrets
import signal
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from coder_eval.agent import Agent
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.models import (
    AGENT_HOME,
    CONTAINER_DROP_SHIM,
    AgentState,
    ApiRoute,
    BaseAgentConfig,
    BedrockRoute,
    DirectRoute,
    LiteLLMRoute,
    TurnRecord,
)
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import StreamEvent
from coder_eval.streaming.wire import deserialize_event, serialize_event
from coder_eval.utils import SKIP, serialize_value


logger = logging.getLogger(__name__)

_RPC_PREFIX = "\x1ecoder-eval-agent-rpc\x1e:"
_MAX_LINE_BYTES = 64 * 1024 * 1024
_STOP_TIMEOUT_SECONDS = 5.0
_SAFE_CODER_EVAL_ENV = frozenset({"CODER_EVAL_IN_CONTAINER"})
_SCRUB_ENV_VARS = frozenset({"AWS_BEARER_TOKEN_BEDROCK", "SKILLS_REPO_PATH", "TASK_DIR"})
_LINUX_CAPABILITY_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")


def build_agent_worker_environment() -> dict[str, str]:
    """Return the least-privilege environment inherited by the agent worker."""

    env = dict(os.environ)
    for name in list(env):
        if name in _SCRUB_ENV_VARS or (name.startswith("CODER_EVAL_") and name not in _SAFE_CODER_EVAL_ENV):
            env.pop(name, None)
    env.update(
        {
            "HOME": AGENT_HOME,
            "LOGNAME": "agent",
            "USER": "agent",
            "ZDOTDIR": AGENT_HOME,
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
    )
    return env


def _linux_security_status() -> dict[str, Any]:
    """Read the kernel-enforced privilege state used by the startup handshake."""

    try:
        fields = {
            name: value
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
            if ":" in line
            for name, value in [line.split(":", 1)]
        }
        uids = [int(value) for value in fields["Uid"].split()]
        gids = [int(value) for value in fields["Gid"].split()]
        groups = [int(value) for value in fields["Groups"].split()]
        capabilities = {name: int(fields[name].strip(), 16) for name in _LINUX_CAPABILITY_FIELDS}
        no_new_privs = int(fields["NoNewPrivs"].strip())
    except (KeyError, OSError, ValueError):
        return {"uids": None, "gids": None, "groups": None, "capabilities": None, "no_new_privs": None}
    return {
        "uids": uids,
        "gids": gids,
        "groups": groups,
        "capabilities": capabilities,
        "no_new_privs": no_new_privs,
    }


def _route_to_payload(route: ApiRoute | None) -> dict[str, Any] | None:
    if route is None:
        return None
    return {"type": type(route).__name__, "data": dataclasses.asdict(route)}


def _route_from_payload(payload: dict[str, Any] | None) -> ApiRoute | None:
    if payload is None:
        return None
    route_types: dict[str, type[DirectRoute] | type[BedrockRoute] | type[LiteLLMRoute]] = {
        "DirectRoute": DirectRoute,
        "BedrockRoute": BedrockRoute,
        "LiteLLMRoute": LiteLLMRoute,
    }
    route_type = route_types.get(str(payload.get("type")))
    if route_type is None:
        raise ValueError(f"unknown agent worker route type: {payload.get('type')!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError("agent worker route data must be an object")
    return route_type(**data)


def _json_safe(value: Any) -> Any:
    encoded = serialize_value(value)
    return None if encoded is SKIP else encoded


def _snapshot(agent: Agent[Any] | None) -> dict[str, Any]:
    if agent is None:
        return {"state": AgentState.FINISHED.value, "pending_turn": None, "sdk_options": None, "environment": {}}
    pending = agent.pending_turn
    return {
        "state": agent.get_state().value,
        "pending_turn": pending.model_dump(mode="json") if pending is not None else None,
        "sdk_options": _json_safe(agent.get_sdk_options()),
        "environment": _json_safe(agent.get_environment_info()),
    }


def _error_snapshot(agent: Agent[Any] | None) -> dict[str, Any]:
    """Preserve failure metadata even when an optional agent getter is broken."""

    if agent is None:
        return _snapshot(None)
    try:
        return _snapshot(agent)
    except Exception:
        logger.warning("Agent metadata getters failed while reporting a worker error", exc_info=True)
        pending = agent.pending_turn
        state = agent.get_state()
        return {
            "state": state.value,
            "pending_turn": pending.model_dump(mode="json") if pending is not None else None,
            "sdk_options": None,
            "environment": {},
        }


def _error_payload(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if isinstance(exc, TurnTimeoutError):
        details = {
            "timeout_seconds": exc.timeout_seconds,
            "task_id": exc.task_id,
            "iteration": exc.iteration,
        }
    return {"type": type(exc).__name__, "message": str(exc), "details": details}


def _new_stop_path() -> Path:
    """Return an absent flag inside a directory writable only by the root proxy."""

    directory = Path(tempfile.mkdtemp(prefix="coder-eval-agent-stop-", dir="/tmp"))
    directory.chmod(0o711)
    return directory / "stop"


def _remove_stop_path(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()


class _WorkerWriter:
    """Nonce-framed writer shared by RPC responses and stream callbacks."""

    def __init__(self, nonce: str) -> None:
        self._prefix = f"{_RPC_PREFIX}{nonce}:"
        self._lock = threading.Lock()

    def write(self, payload: dict[str, Any]) -> None:
        line = self._prefix + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock, contextlib.suppress(BrokenPipeError, OSError):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def on_event(self, event: StreamEvent) -> None:
        self.write({"kind": "event", "event": serialize_event(event)})


class _WorkerServer:
    def __init__(self) -> None:
        self.agent: Agent[Any] | None = None

    async def handle(self, method: str, params: dict[str, Any]) -> tuple[Any, bool]:
        if method == "ping":
            get_euid = getattr(os, "geteuid", None)
            get_egid = getattr(os, "getegid", None)
            uid = get_euid() if callable(get_euid) else None
            gid = get_egid() if callable(get_egid) else None
            return {"uid": uid, "gid": gid, **_linux_security_status()}, False

        if method == "start":
            if self.agent is not None:
                with contextlib.suppress(Exception):
                    await self.agent.stop()
            from coder_eval.agents import AgentRegistry, create_agent
            from coder_eval.plugins import ensure_plugins_loaded

            ensure_plugins_loaded()
            kind = str(params["agent_kind"])
            registration = AgentRegistry.get(kind)
            if registration is None:
                raise AgentRegistry.unregistered_kind_error(kind)
            config = registration.config_class.model_validate(params["config"])
            route = _route_from_payload(params.get("route"))
            kwargs = params.get("constructor_kwargs") or {}
            if not isinstance(kwargs, dict):
                raise TypeError("agent worker constructor_kwargs must be an object")
            self.agent = create_agent(kind, config, route=route, **kwargs)
            await self.agent.start(
                str(params["working_directory"]),
                env_path_prepend=list(params.get("env_path_prepend") or []),
                plugin_tools_dir=params.get("plugin_tools_dir"),
            )
            return _snapshot(self.agent), False

        if self.agent is None:
            raise RuntimeError(f"agent worker received {method!r} before start")

        if method == "communicate":
            stop_path_raw = params.get("stop_path")
            stop_path = Path(stop_path_raw) if isinstance(stop_path_raw, str) else None
            writer = params.pop("_writer")
            record = await self.agent.communicate(
                str(params["user_input"]),
                stream_callback=writer,
                timeout=params.get("timeout"),
                max_turns=params.get("max_turns"),
                should_stop=(lambda: stop_path.exists()) if stop_path is not None else None,
            )
            return {"record": record.model_dump(mode="json"), "snapshot": _snapshot(self.agent)}, False

        if method == "discard_pending_turn":
            await self.agent.discard_pending_turn()
            return _snapshot(self.agent), False

        if method == "stop":
            await self.agent.stop()
            result = _snapshot(self.agent)
            self.agent = None
            return result, True

        raise ValueError(f"unknown agent worker method: {method!r}")

    async def close(self) -> None:
        if self.agent is not None:
            with contextlib.suppress(Exception):
                await self.agent.stop()
            self.agent = None


async def _serve_worker() -> None:
    """Serve nonce-authenticated JSON requests from stdin until stop or EOF."""

    server = _WorkerServer()
    nonce: str | None = None
    writer: _WorkerWriter | None = None
    try:
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline):
            request = json.loads(raw)
            request_nonce = request.get("nonce")
            if nonce is None:
                if not isinstance(request_nonce, str) or len(request_nonce) < 32:
                    raise ValueError("agent worker handshake is missing a strong nonce")
                nonce = request_nonce
                writer = _WorkerWriter(nonce)
            elif request_nonce != nonce:
                continue

            assert writer is not None
            request_id = request.get("id")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                writer.write(
                    {
                        "kind": "response",
                        "id": request_id,
                        "ok": False,
                        "error": {"type": "TypeError", "message": "request params must be an object", "details": {}},
                        "snapshot": _snapshot(server.agent),
                    }
                )
                continue
            if request.get("method") == "communicate":
                params["_writer"] = writer
            should_exit = False
            try:
                result, should_exit = await server.handle(str(request.get("method")), params)
                writer.write({"kind": "response", "id": request_id, "ok": True, "result": result})
            except Exception as exc:
                writer.write(
                    {
                        "kind": "response",
                        "id": request_id,
                        "ok": False,
                        "error": _error_payload(exc),
                        "snapshot": _error_snapshot(server.agent),
                    }
                )
            if should_exit:
                return
    finally:
        await server.close()


def agent_worker_internal_command() -> None:
    """Run the private agent-worker protocol on stdin/stdout."""

    asyncio.run(_serve_worker())


class IsolatedAgentProxy(Agent[BaseAgentConfig]):
    """Root-side ``Agent`` implementation backed by one dropped-UID worker."""

    def __init__(
        self,
        agent_kind: str,
        config: BaseAgentConfig,
        *,
        route: ApiRoute | None,
        constructor_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.agent_kind = agent_kind
        self.config = config
        self.route = route
        self.constructor_kwargs = constructor_kwargs or {}
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._nonce = secrets.token_hex(32)
        self._next_request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sdk_options: dict[str, Any] | None = None
        self._environment_info: dict[str, Any] = {}
        self._active_collector: EventCollector | None = None
        self._active_callback: StreamCallback | None = None
        self._active_should_stop: Callable[[], bool] | None = None
        self._active_stop_path: Path | None = None
        self._active_event_seen = False

    @property
    def _response_prefix(self) -> str:
        return f"{_RPC_PREFIX}{self._nonce}:"

    async def _spawn(self, working_directory: str) -> None:
        from coder_eval.isolation.agent_identity import require_isolation_runtime

        require_isolation_runtime()
        self._process = await asyncio.create_subprocess_exec(
            CONTAINER_DROP_SHIM,
            sys.executable,
            "-I",
            "-m",
            "coder_eval.isolation.agent_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
            env=build_agent_worker_environment(),
            start_new_session=True,
            limit=_MAX_LINE_BYTES,
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        hello = await self._request("ping", {})
        from coder_eval.models import AGENT_GID, AGENT_UID

        capabilities = hello.get("capabilities") if isinstance(hello, dict) else None
        privilege_drop_ok = (
            isinstance(hello, dict)
            and hello.get("uid") == AGENT_UID
            and hello.get("gid") == AGENT_GID
            and hello.get("uids") == [AGENT_UID] * 4
            and hello.get("gids") == [AGENT_GID] * 4
            and hello.get("groups") == []
            and hello.get("no_new_privs") == 1
            and isinstance(capabilities, dict)
            and set(capabilities) == set(_LINUX_CAPABILITY_FIELDS)
            and all(value == 0 for value in capabilities.values())
        )
        if not privilege_drop_ok:
            await self.kill()
            raise RuntimeError(f"agent worker did not enter the configured unprivileged security domain: {hello!r}")

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while raw := await process.stdout.readline():
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith(self._response_prefix):
                    logger.info("[agent-worker] %s", line)
                    continue
                try:
                    payload = json.loads(line[len(self._response_prefix) :])
                except json.JSONDecodeError as exc:
                    raise ValueError("malformed agent-worker protocol line") from exc
                if payload.get("kind") == "event":
                    event = deserialize_event(str(payload.get("event", "")))
                    if event is None:
                        raise ValueError("invalid event in agent-worker protocol")
                    self._active_event_seen = True
                    if self._active_collector is not None:
                        self._active_collector.on_event(event)
                    safe_emit(self._active_callback, event)
                    self._publish_stop_flag_if_needed()
                    continue
                if payload.get("kind") != "response":
                    raise ValueError("unknown agent-worker protocol message")
                request_id = payload.get("id")
                future = self._pending.get(request_id) if isinstance(request_id, int) else None
                if future is None:
                    raise ValueError(f"agent-worker response has no pending request: {request_id!r}")
                if not future.done():
                    future.set_result(payload)
        except (ValueError, asyncio.IncompleteReadError) as exc:
            logger.error("Agent-worker output protocol failed: %s", exc)
            self.kill_sync()
        finally:
            error = AgentCrashError("isolated agent worker exited before completing the request")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            try:
                raw = await process.stderr.readline()
            except ValueError:
                logger.warning("Dropped an agent-worker stderr line over %d bytes", _MAX_LINE_BYTES)
                continue
            if not raw:
                return
            logger.info("[agent-worker] %s", raw.decode("utf-8", errors="replace").rstrip())

    def _publish_stop_flag_if_needed(self) -> None:
        if self._active_should_stop is None or self._active_stop_path is None:
            return
        try:
            if self._active_should_stop():
                self._active_stop_path.touch(exist_ok=True)
        except Exception:
            logger.warning("Agent early-stop callback failed (ignored)", exc_info=True)

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AgentCrashError("isolated agent worker is not running")
        self._next_request_id += 1
        request_id = self._next_request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"nonce": self._nonce, "id": request_id, "method": method, "params": params}
        try:
            process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if not response.get("ok"):
            self._apply_snapshot(response.get("snapshot"))
            self._raise_remote_error(response.get("error"))
        return response.get("result")

    def _apply_snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            return
        state = snapshot.get("state")
        if isinstance(state, str):
            with contextlib.suppress(ValueError):
                self._state = AgentState(state)
        pending = snapshot.get("pending_turn")
        self.pending_turn = TurnRecord.model_validate(pending) if isinstance(pending, dict) else None
        sdk_options = snapshot.get("sdk_options")
        self._sdk_options = sdk_options if isinstance(sdk_options, dict) else None
        environment = snapshot.get("environment")
        self._environment_info = environment if isinstance(environment, dict) else {}

    @staticmethod
    def _raise_remote_error(error: Any) -> NoReturn:
        if not isinstance(error, dict):
            raise AgentCrashError("isolated agent worker returned an invalid error")
        error_type = str(error.get("type"))
        message = str(error.get("message", "isolated agent worker failed"))
        details_raw = error.get("details")
        details = details_raw if isinstance(details_raw, dict) else {}
        if error_type == "TurnTimeoutError":
            raise TurnTimeoutError(
                float(details.get("timeout_seconds", 0)),
                task_id=details.get("task_id"),
                iteration=details.get("iteration"),
            )
        exception_types: dict[str, type[Exception]] = {
            "AgentConfigError": AgentConfigError,
            "AgentCrashError": AgentCrashError,
            "FileNotFoundError": FileNotFoundError,
            "ImportError": ImportError,
            "RuntimeError": RuntimeError,
            "TypeError": TypeError,
            "ValueError": ValueError,
        }
        exception_type = exception_types.get(error_type, AgentCrashError)
        raise exception_type(message)

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        if self._process is None or self._process.returncode is not None:
            await self._spawn(working_directory)
        result = await self._request(
            "start",
            {
                "agent_kind": self.agent_kind,
                "config": self.config.model_dump(mode="json"),
                "route": _route_to_payload(self.route),
                "constructor_kwargs": self.constructor_kwargs,
                "working_directory": working_directory,
                "env_path_prepend": env_path_prepend or [],
                "plugin_tools_dir": plugin_tools_dir,
            },
        )
        self._apply_snapshot(result)

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        collector = EventCollector()
        stop_path: Path | None = None
        if should_stop is not None:
            stop_path = await asyncio.to_thread(_new_stop_path)
        self._active_collector = collector
        self._active_callback = stream_callback
        self._active_should_stop = should_stop
        self._active_stop_path = stop_path
        self._active_event_seen = False
        try:
            result = await self._request(
                "communicate",
                {
                    "user_input": user_input,
                    "timeout": timeout,
                    "max_turns": max_turns,
                    "stop_path": str(stop_path) if stop_path else None,
                },
            )
            if not isinstance(result, dict):
                raise AgentCrashError("isolated agent worker returned an invalid turn result")
            self._apply_snapshot(result.get("snapshot"))
            return TurnRecord.model_validate(result.get("record"))
        except BaseException:
            if self.pending_turn is None and self._active_event_seen:
                partial = collector.build_turn_record()
                self.pending_turn = partial.model_copy(
                    update={"crashed": True, "crash_reason": "agent worker terminated"}
                )
            raise
        finally:
            self._active_collector = None
            self._active_callback = None
            self._active_should_stop = None
            self._active_stop_path = None
            if stop_path is not None:
                await asyncio.to_thread(_remove_stop_path, stop_path)

    async def discard_pending_turn(self) -> None:
        if self._process is None or self._process.returncode is not None:
            await super().discard_pending_turn()
            return
        result = await self._request("discard_pending_turn", {})
        self._apply_snapshot(result)

    async def stop(self) -> None:
        process = self._process
        if process is None:
            self._mark_stopped()
            return
        if process.returncode is None:
            try:
                result = await asyncio.wait_for(self._request("stop", {}), timeout=_STOP_TIMEOUT_SECONDS)
                self._apply_snapshot(result)
            except Exception:
                logger.warning("Agent worker did not stop cleanly; terminating its process group", exc_info=True)
                await self.kill()
        await self._finish_process_tasks()
        self._process = None
        self._mark_stopped()

    def kill_sync(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            kill_process_group = getattr(os, "killpg", None)
            if not callable(kill_process_group):
                raise AttributeError("os.killpg is unavailable")
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill_process_group(process.pid, sigkill)
        except (AttributeError, OSError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    async def kill(self) -> None:
        process = self._process
        self.kill_sync()
        if process is not None:
            with contextlib.suppress(Exception):
                await process.wait()
        await self._finish_process_tasks()

    async def _finish_process_tasks(self) -> None:
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
        self._stdout_task = None
        self._stderr_task = None

    def get_sdk_options(self) -> dict[str, Any] | None:
        return self._sdk_options

    def get_environment_info(self) -> dict[str, Any]:
        return dict(self._environment_info)


if __name__ == "__main__":
    agent_worker_internal_command()
