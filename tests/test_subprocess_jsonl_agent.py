"""``SubprocessJsonlAgent``: the shared nd-JSON CLI transport, against real child processes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest

from coder_eval.agents._transport import JsonlDecoder, SubprocessJsonlAgent, subprocess_jsonl
from coder_eval.models import Enforcement, HarnessContract, PiAgentConfig, TimingBasis, UsageGranularity
from coder_eval.streaming.emitter import TurnOutcome
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, StopReason, StreamEvent, end_status_for


pytestmark = pytest.mark.skipif(os.name != "posix", reason="process-group teardown is POSIX-only")


class _Decoder(JsonlDecoder):
    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("type") == "say":
            self.emitter.text(str(event.get("text", "")))
        elif event.get("type") == "err":
            self.error = str(event.get("message"))

    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(status, reason or status.value)
        return self.emitter.finalize(status)


class _ScriptAgent(SubprocessJsonlAgent[PiAgentConfig]):
    """Runs ``python -c <script>`` as its CLI."""

    contract = HarnessContract(
        system_prompt=Enforcement.UNSUPPORTED,
        plugin_skills=Enforcement.UNSUPPORTED,
        permission_mode=Enforcement.UNSUPPORTED,
        allowed_tools=Enforcement.UNSUPPORTED,
        disallowed_tools=Enforcement.UNSUPPORTED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.TURN,
        timing_basis=TimingBasis.TURN_CLOCK,
    )
    cli_name = "Script"
    docs_page = "docs/agents/SCRIPT.md"
    recognized_events = frozenset({"say", "err"})
    decoder = _Decoder

    def __init__(self, script: str) -> None:
        super().__init__(PiAgentConfig(type="pi", model="m"), task_id="t")
        self.script = script

    def argv(self, prompt: str) -> list[str]:
        return [sys.executable, "-c", self.script, prompt]

    def env(self) -> dict[str, str]:
        return dict(os.environ)

    async def start(self, working_directory: str, **_: Any) -> None:
        self.working_directory = working_directory

    async def stop(self) -> None:
        await self.kill()
        self._mark_stopped()


class _Recorder:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)

    def ends(self) -> list[AgentEndEvent]:
        return [e for e in self.events if isinstance(e, AgentEndEvent)]


def _say(text: str) -> str:
    return f"print({json.dumps(json.dumps({'type': 'say', 'text': text}))}, flush=True)"


async def _run(script: str, tmp_path: Any, **kwargs: Any) -> tuple[TurnOutcome, _Recorder, _ScriptAgent]:
    agent = _ScriptAgent(script)
    await agent.start(str(tmp_path))
    recorder = _Recorder()
    outcome = await agent.communicate("go", iteration=1, stream_callback=recorder, **kwargs)
    return outcome, recorder, agent


def _group_is_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class TestTheTurn:
    async def test_a_child_that_reads_stdin_to_eof_completes(self, tmp_path):
        script = f"import sys\nsys.stdin.read()\n{_say('read stdin')}"
        outcome, recorder, _ = await _run(script, tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.COMPLETED
        assert outcome.record.agent_output == "read stdin"
        assert len(recorder.ends()) == 1

    async def test_a_line_past_the_default_stream_limit_is_read(self, tmp_path):
        outcome, _, _ = await _run(_say("x" * 100_000), tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.COMPLETED
        assert len(outcome.record.agent_output) == 100_000

    async def test_a_stderr_flood_does_not_hang_the_turn(self, tmp_path):
        script = f"import sys\nsys.stderr.write('e' * 200_000)\nsys.stderr.flush()\n{_say('done')}"
        outcome, _, _ = await _run(script, tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.COMPLETED

    async def test_a_non_zero_exit_crashes_with_stderr(self, tmp_path):
        script = f"import sys\n{_say('partial')}\nsys.stderr.write('boom: bad model')\nsys.exit(3)"
        outcome, recorder, _ = await _run(script, tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "boom: bad model" in outcome.error
        assert outcome.record.crashed is True
        assert [e.status for e in recorder.ends()] == [AgentEndStatus.CRASHED]

    async def test_a_decoder_error_crashes_the_turn(self, tmp_path):
        script = f"print({json.dumps(json.dumps({'type': 'err', 'message': 'provider 401'}))}, flush=True)"
        outcome, _, _ = await _run(script, tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error == "Script error: provider 401"

    async def test_a_clean_exit_with_no_recognized_event_is_drift(self, tmp_path):
        script = f"print({json.dumps(json.dumps({'type': 'mystery'}))}, flush=True)\nprint('not json', flush=True)"
        outcome, _, _ = await _run(script, tmp_path, timeout=20)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no recognized events" in outcome.error and "mystery" in outcome.error
        assert "docs/agents/SCRIPT.md" in outcome.error

    async def test_the_deadline_is_a_timeout_and_the_process_group_is_gone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess_jsonl, "_TERM_GRACE_SECONDS", 0.5)
        script = f"import subprocess, time\nsubprocess.Popen(['sleep', '60'])\n{_say('started')}\ntime.sleep(60)"
        agent = _ScriptAgent(script)
        await agent.start(str(tmp_path))
        spawned: list[int] = []
        original = asyncio.create_subprocess_exec

        async def recording_exec(*args: Any, **kwargs: Any) -> Any:
            proc = await original(*args, **kwargs)
            spawned.append(proc.pid)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
        recorder = _Recorder()
        outcome = await agent.communicate("go", iteration=1, stream_callback=recorder, timeout=1.0)
        assert outcome.status is AgentEndStatus.TIMEOUT
        assert outcome.record.agent_output == "started"
        assert [e.status for e in recorder.ends()] == [AgentEndStatus.TIMEOUT]
        for _ in range(100):
            if _group_is_gone(spawned[0]):
                break
            await asyncio.sleep(0.05)
        assert _group_is_gone(spawned[0]), "the grandchild survived: the process group was not swept"

    async def test_a_stop_after_the_first_line_kills_and_ends_cleanly(self, tmp_path):
        script = f"import time\n{_say('one')}\n{_say('two')}\ntime.sleep(60)"
        polls: list[int] = []

        def should_stop() -> StopReason | None:
            polls.append(1)
            return StopReason.TOOL_CALL_CAP

        outcome, recorder, _ = await _run(script, tmp_path, timeout=20, should_stop=should_stop)
        assert outcome.status is end_status_for(StopReason.TOOL_CALL_CAP)
        assert outcome.record.crashed is False
        assert outcome.record.agent_output == "one"
        assert polls == [1]
        assert len(recorder.ends()) == 1

    async def test_a_cancel_ends_the_turn_once_and_propagates(self, tmp_path):
        script = f"import time\n{_say('working')}\ntime.sleep(60)"
        agent = _ScriptAgent(script)
        await agent.start(str(tmp_path))
        recorder = _Recorder()
        task = asyncio.ensure_future(agent.communicate("go", iteration=1, stream_callback=recorder, timeout=30))
        for _ in range(200):
            if any(getattr(e, "text", None) == "working" for e in recorder.events):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ends = recorder.ends()
        assert [(e.status, e.crash_reason) for e in ends] == [(AgentEndStatus.CRASHED, "turn cancelled")]
        assert agent._process is None
