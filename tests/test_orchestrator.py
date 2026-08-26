"""Tests for the orchestrator."""

import hashlib
import os
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    BedrockRoute,
    ClaudeCodeAgentConfig,
    DirectRoute,
    FileExistsCriterion,
    LiteLLMRoute,
    PreservationMode,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.task_loader import load_task
from coder_eval.orchestrator import Orchestrator, _format_routing
from coder_eval.sandbox import Sandbox
from coder_eval.utils import get_version_info
from tests._path_helpers import tmp_subdir
from tests.fixtures.mock_agent import MockAgent


# Cross-platform stand-in for the literal _TEST_CWD cwd used by SDK-option
# round-trip tests — the value only needs to be a stable opaque string that
# survives serialisation; same value is used on both ends of every assertion.
_TEST_CWD = str(tmp_subdir("test"))
_SANDBOX_CWD = str(tmp_subdir("sandbox"))


def test_format_routing_direct_includes_judge_transport_anthropic():
    assert _format_routing(DirectRoute(judge_transport="anthropic")) == (
        "anthropic_direct (judge transport: anthropic)"
    )


def test_format_routing_direct_judge_transport_none_renders_as_none():
    """Unset transport prints 'none' so log readers don't see a confusing 'None' literal."""
    assert _format_routing(DirectRoute(judge_transport=None)) == "anthropic_direct (judge transport: none)"


def test_format_routing_non_direct_routes_unchanged():
    """BedrockRoute keeps the original bare-name format — judge transport is a Direct-only concern."""
    assert _format_routing(BedrockRoute(region="us-east-1")) == "aws_bedrock"


def test_format_routing_litellm_shows_model():
    out = _format_routing(LiteLLMRoute(model="zai.glm-5"))
    assert out.startswith("litellm")
    assert "zai.glm-5" in out


def test_format_routing_litellm_effective_model_wins_over_route_default():
    """The --model override (effective_model) must be logged, not the route's LITELLM_MODEL default."""
    out = _format_routing(
        LiteLLMRoute(model="zai.glm-5"),
        effective_model="deepseek.v3.2",
    )
    assert "deepseek.v3.2" in out
    assert "zai.glm-5" not in out


def _make_orchestrator_with_route(tmp_path: Path, route) -> Orchestrator:
    """Build a minimal Orchestrator pre-populated with a route + EvaluationResult.

    Bypasses ``_setup`` so unit tests can exercise ``_record_route_environment_info``
    in isolation across both run modes.
    """
    from coder_eval.models import EvaluationResult, FinalStatus

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="t")
    orchestrator.route = route
    assert task.agent is not None
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        agent_type=task.agent.type,
        started_at=0.0,
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
        environment_info={},
    )
    return orchestrator


def test_record_route_environment_info_direct_writes_judge_transport(tmp_path):
    orchestrator = _make_orchestrator_with_route(tmp_path, DirectRoute(judge_transport="anthropic"))
    orchestrator._record_route_environment_info()
    assert orchestrator.result is not None
    assert orchestrator.result.environment_info["api_routing"] == "anthropic_direct"
    assert orchestrator.result.environment_info["judge_transport"] == "anthropic"


def test_record_route_environment_info_direct_none_serialized_as_string(tmp_path):
    """judge_transport=None must surface as the string 'none' (audit-readable, never a literal None)."""
    orchestrator = _make_orchestrator_with_route(tmp_path, DirectRoute(judge_transport=None))
    orchestrator._record_route_environment_info()
    assert orchestrator.result is not None
    assert orchestrator.result.environment_info["judge_transport"] == "none"


def test_record_route_environment_info_bedrock(tmp_path):
    orchestrator = _make_orchestrator_with_route(
        tmp_path, BedrockRoute(region="eu-north-1", model="eu.anthropic.claude-sonnet-4-6")
    )
    orchestrator._record_route_environment_info()
    assert orchestrator.result is not None
    info = orchestrator.result.environment_info
    assert info["api_routing"] == "aws_bedrock"
    assert info["aws_region"] == "eu-north-1"
    assert info["bedrock_model"] == "eu.anthropic.claude-sonnet-4-6"


def test_record_route_environment_info_litellm_records_host_only_no_secret(tmp_path, monkeypatch):
    """LiteLLM route records host + model, but NEVER the auth token or full base_url."""
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "litellm_base_url", "http://localhost:4000")
    orchestrator = _make_orchestrator_with_route(
        tmp_path,
        LiteLLMRoute(model="zai.glm-5"),
    )
    orchestrator._record_route_environment_info()
    assert orchestrator.result is not None
    info = orchestrator.result.environment_info
    assert info["api_routing"] == "litellm"
    assert info["litellm_base_url_host"] == "localhost"
    assert info["litellm_model"] == "zai.glm-5"
    # No secret and no full URL anywhere in the recorded audit dict.
    blob = str(info)
    assert "sk-super-secret" not in blob
    assert "http://localhost:4000" not in blob
    assert "judge_transport" not in info  # Direct-only field


def test_finalize_result_score_mismatch_marks_error_and_writes_task_json(tmp_path):
    """A weighted-score length mismatch at finalize becomes a visible ERROR row, task.json still written."""
    from coder_eval.models import CriterionResult, EvaluationResult, FinalStatus

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    # hello_date carries 3 criteria; injecting a single result forces the mismatch.
    assert len(task.success_criteria) != 1

    run_dir = tmp_path / "run"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="t")
    assert task.agent is not None
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        agent_type=task.agent.type,
        started_at=0.0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        success_criteria_results=[
            CriterionResult(criterion_type="file_exists", description="A", score=1.0),
        ],
        environment_info={},
    )

    orchestrator._finalize_result(start_time=0.0)

    assert orchestrator.result.final_status == FinalStatus.ERROR
    assert orchestrator.result.weighted_score is None
    assert orchestrator.result.error_details is not None
    # task.json is still persisted despite the failed score computation.
    assert orchestrator.report_path.exists()


class _SdkOptionsAgent(MockAgent):
    """``MockAgent`` subclass that returns a configurable ``get_sdk_options``.

    Used by the PATH-sync tests so the dummy agent satisfies the full
    ``Agent`` ABC (``start`` / ``communicate`` / ``stop`` / ``get_state``)
    rather than only the one method ``_sync_…`` happens to call today —
    keeps the test surface aligned with the production contract.
    """

    def __init__(self, task, sdk_options):
        super().__init__(task)
        self._sdk_options = sdk_options

    def get_sdk_options(self):
        return self._sdk_options


class _AsyncSdkOptionsAgent(MockAgent):
    """Returns a fresh coroutine every call — mimics ``AsyncMock`` leakage."""

    def get_sdk_options(self):
        async def _coro():
            return {"env": {"PATH": "/agent/bin"}}

        return _coro()


@pytest.fixture
def path_sync_orchestrator(tmp_path):
    """Yield ``(orchestrator, task)`` ready for PATH-sync helper tests.

    Removes the boilerplate (load task, build orchestrator, setup sandbox,
    cleanup in finally) that the per-test body would otherwise repeat.
    """
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox = SandboxConfig(driver="tempdir", python=None)
    orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="t")
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    orchestrator.sandbox.setup()
    try:
        yield orchestrator, task
    finally:
        orchestrator.sandbox.cleanup()


def test_sync_sandbox_command_path_from_agent_sdk_options(path_sync_orchestrator, monkeypatch, tmp_path):
    """Happy path: agent SDK PATH wins for criteria ``run_command`` resolution."""
    from tests._path_helpers import write_uip_shim

    orchestrator, task = path_sync_orchestrator
    stale_bin = tmp_path / "stale"
    agent_bin = tmp_path / "agent"
    stale_bin.mkdir()
    agent_bin.mkdir()
    write_uip_shim(stale_bin, "stale")
    write_uip_shim(agent_bin, "agent")
    monkeypatch.setenv("PATH", str(stale_bin))

    orchestrator.agent = _SdkOptionsAgent(task, {"env": {"PATH": f"{agent_bin}{os.pathsep}{stale_bin}"}})
    orchestrator._sync_sandbox_command_path_with_agent()

    exit_code, stdout, _stderr = orchestrator.sandbox.run_command("uip")
    assert exit_code == 0
    assert stdout.strip() == "agent"


def test_sync_sandbox_command_path_preserves_host_path_for_system_bins(path_sync_orchestrator, monkeypatch, tmp_path):
    """The agent's narrow PATH must not clobber the host PATH for system bins.

    Locks in the prepend (not replace) semantics flagged HIGH in the
    multi-model review (PR #249 thread). If a future refactor accidentally
    re-introduces ``env['PATH'] = base_path`` (replace), this fails because
    the host's ``/usr/bin``-style binary becomes unreachable.
    """
    from tests._path_helpers import write_uip_shim

    orchestrator, task = path_sync_orchestrator
    # Host PATH carries a `uip` named "host". Agent PATH carries no `uip`.
    host_bin = tmp_path / "host"
    agent_bin = tmp_path / "agent"  # intentionally empty
    host_bin.mkdir()
    agent_bin.mkdir()
    write_uip_shim(host_bin, "host")
    monkeypatch.setenv("PATH", str(host_bin))

    orchestrator.agent = _SdkOptionsAgent(task, {"env": {"PATH": str(agent_bin)}})
    orchestrator._sync_sandbox_command_path_with_agent()

    # Agent PATH wins for binaries it provides; falls through to host PATH
    # for binaries it does not. A replace-style implementation would return
    # exit_code == 1 with the "not found" message.
    exit_code, stdout, _stderr = orchestrator.sandbox.run_command("uip")
    assert exit_code == 0
    assert stdout.strip() == "host"


def test_sync_sandbox_command_path_awaitable_sdk_options_is_closed_and_noops(path_sync_orchestrator, caplog):
    """AsyncMock-style coroutine returns: close, do not leak warnings."""
    orchestrator, task = path_sync_orchestrator
    orchestrator.agent = _AsyncSdkOptionsAgent(task)
    with caplog.at_level("DEBUG", logger="coder_eval.orchestrator"):
        orchestrator._sync_sandbox_command_path_with_agent()
    assert orchestrator.sandbox.command_base_path is None
    # Logged at DEBUG (test-fixture concern), not WARNING.
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("awaitable" in r.message for r in debug_records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize(
    "sdk_options, warn_substring",
    [
        pytest.param(None, None, id="none-sdk-options"),
        pytest.param(["unexpected"], "non-dict", id="non-dict-sdk-options"),
        pytest.param({"env": {"HOME": "/tmp"}}, None, id="missing-path-key"),
        pytest.param({"env": "not-a-dict"}, None, id="env-not-a-dict"),
    ],
)
def test_sync_sandbox_command_path_contract_edge_cases_are_noops(
    path_sync_orchestrator, caplog, sdk_options, warn_substring
):
    """Edge inputs leave ``command_base_path`` unset, with the right log level."""
    orchestrator, task = path_sync_orchestrator
    orchestrator.agent = _SdkOptionsAgent(task, sdk_options)
    with caplog.at_level("WARNING", logger="coder_eval.orchestrator"):
        orchestrator._sync_sandbox_command_path_with_agent()
    assert orchestrator.sandbox.command_base_path is None
    if warn_substring is None:
        # Silent no-op — these are valid pre-communicate / sparse-env states.
        assert not any(r.levelname == "WARNING" for r in caplog.records)
    else:
        # Contract violation — must be visible at WARNING.
        assert any(r.levelname == "WARNING" and warn_substring in r.message for r in caplog.records)


def test_orchestrator_load_task():
    """Test loading a task from YAML."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    assert task.task_id == "hello_date_smoke_test"
    assert len(task.success_criteria) == 3


def test_orchestrator_load_task_missing_file():
    """Test loading a non-existent task file."""
    with pytest.raises(FileNotFoundError):
        load_task(Path("tasks/nonexistent.yaml"))


def test_orchestrator_load_task_directory():
    """Test that loading a directory instead of a YAML file gives a clear error."""
    with pytest.raises(ValueError, match="Expected a YAML task file but got a directory"):
        load_task(Path("tasks"))


def test_orchestrator_initialization(tmp_path):
    """Test orchestrator initialization."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    run_dir = tmp_path / "test_run" / "hello_date"

    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.NONE, variant_id="test-variant"
    )

    assert orchestrator.task == task
    assert orchestrator.run_dir == run_dir
    assert orchestrator.sandbox is None
    assert orchestrator.agent is None
    assert orchestrator.result is None


@pytest.mark.asyncio
async def test_orchestrator_create_agent(tmp_path):
    """Test agent creation."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    run_dir = tmp_path / "test_run" / "hello_date"

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.route = DirectRoute()

    # Create agent
    agent = await orchestrator._create_agent()

    assert agent is not None
    assert agent.config.type == "claude-code"


# ============================================================================
# Batch Orchestration Tests (Phase 1)
# ============================================================================


def create_test_task_file(tmp_path: Path, task_id: str) -> Path:
    """Helper to create a valid test task YAML file."""
    task_content = f"""
task_id: {task_id}
description: Test task for batch execution
initial_prompt: "Test prompt"
agent:
  type: claude-code
sandbox:
  driver: tempdir
  python: {{}}
success_criteria:
  - type: file_exists
    path: test.txt
    description: "Check for test.txt"
"""
    task_file = tmp_path / f"{task_id}.yaml"
    task_file.write_text(task_content)
    return task_file


@pytest.mark.asyncio
async def test_run_batch_empty_list(tmp_path):
    """Test batch execution with empty task list (edge case from review)."""
    from coder_eval.orchestration.batch import run_batch
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    # Should handle empty list gracefully (empty list of ResolvedTask)
    summary, task_results = await run_batch([], config)

    # Verify empty summary
    assert task_results == []
    assert summary.tasks_run == 0
    assert summary.tasks_succeeded == 0
    assert summary.tasks_failed == 0
    assert summary.tasks_error == 0
    assert len(summary.task_results) == 0

    # Files should still be created
    assert (tmp_path / "run" / "run.json").exists()
    assert (tmp_path / "run" / "run.md").exists()


def test_batch_run_config_validation():
    """Test BatchRunConfig validation."""
    from coder_eval.orchestration.config import BatchRunConfig

    # Valid config
    config = BatchRunConfig(run_dir=tmp_subdir("run"), max_parallel=3)
    assert config.max_parallel == 3

    # Invalid: max_parallel < 1
    with pytest.raises(ValueError):
        BatchRunConfig(run_dir=tmp_subdir("run"), max_parallel=0)

    with pytest.raises(ValueError):
        BatchRunConfig(run_dir=tmp_subdir("run"), max_parallel=-1)


def test_generate_run_summary(tmp_path):
    """Test run summary generation."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    # Create mock results
    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test 1",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                environment_info={},
            ),
            duration=10.0,
        ),
        TaskResult(
            task_id="task2",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task2",
                task_description="Test 2",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="FAILURE",
                iteration_count=2,
                environment_info={},
            ),
            duration=15.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    # Verify summary
    assert summary.tasks_run == 2
    assert summary.tasks_succeeded == 1
    assert summary.tasks_failed == 1
    assert summary.tasks_error == 0

    # Verify files created
    assert (tmp_path / "run.json").exists()
    assert (tmp_path / "run.md").exists()


def test_generate_run_summary_mixed_statuses(tmp_path):
    """Test that tasks_failed excludes ERROR tasks (counted separately in tasks_error)."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="v",
            result=EvaluationResult(
                task_id="task1",
                task_description="ok",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                environment_info={},
            ),
            duration=1.0,
        ),
        TaskResult(
            task_id="task2",
            variant_id="v",
            result=EvaluationResult(
                task_id="task2",
                task_description="fail",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="FAILURE",
                iteration_count=1,
                environment_info={},
            ),
            duration=2.0,
        ),
        TaskResult(
            task_id="task3",
            variant_id="v",
            result=EvaluationResult(
                task_id="task3",
                task_description="err",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="ERROR",
                iteration_count=0,
                environment_info={},
            ),
            duration=0.5,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.tasks_run == 3
    assert summary.tasks_succeeded == 1
    assert summary.tasks_failed == 1  # only FAILURE, not ERROR
    assert summary.tasks_error == 1
    # Invariant: succeeded + failed + error == total
    assert summary.tasks_succeeded + summary.tasks_failed + summary.tasks_error == summary.tasks_run


def test_create_error_result(tmp_path):
    """Test error result creation for failed tasks."""
    from coder_eval.models import TaskResult
    from coder_eval.orchestration.batch import _create_error_task_result

    task_file = tmp_path / "failed_task.yaml"
    error = ValueError("Task loading failed")

    result = _create_error_task_result(task_file, error, variant_id="test-variant")

    # Verify typed result
    assert isinstance(result, TaskResult)
    assert result.task_id == "failed_task"  # Stem of filename
    assert result.duration == 0.0
    assert result.result.final_status == "ERROR"
    assert result.result.error_message == "Task loading failed"
    assert result.result.iteration_count == 0


# ==================== Persistent Sandbox / Cleanup Tests ====================


@pytest.mark.asyncio
async def test_orchestrator_setup_move_on_write_uses_ephemeral_runtime_dir(tmp_path, monkeypatch):
    """Preserved runs should execute in a tempdir, then copy artifacts at cleanup."""
    from datetime import datetime

    from coder_eval import orchestrator as orchestrator_module
    from coder_eval.models import ApiBackend, DirectRoute, EvaluationResult
    from coder_eval.sandbox import Sandbox

    class DummyAgent:
        async def start(
            self,
            working_directory: str,
            *,
            env_path_prepend: list[str] | None = None,
            plugin_tools_dir: str | None = None,
        ) -> None:
            self.working_directory = working_directory

        def get_sdk_options(self):
            return {"env": {"PATH": os.environ.get("PATH", "")}}

        def get_environment_info(self):
            # Non-empty on purpose: pins the orchestrator merge seam
            # (environment_info.update(agent.get_environment_info())) that
            # carries agent markers like system_prompt_semantics into run.json.
            return {"system_prompt_semantics": "append"}

    async def create_dummy_agent(_self):
        return DummyAgent()

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.MOVE_ON_WRITE, variant_id="test-variant"
    )
    orchestrator.task_file = task_file
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    monkeypatch.setattr(orchestrator_module.settings, "api_backend", ApiBackend.DIRECT)
    monkeypatch.setattr(type(orchestrator_module.settings), "validate_api_keys", lambda _self, _agent_type: None)
    monkeypatch.setattr(orchestrator_module, "resolve_route", lambda _settings: DirectRoute(judge_transport=None))
    monkeypatch.setattr(Orchestrator, "_create_agent", create_dummy_agent)

    await orchestrator._setup()

    # An agent-supplied environment_info key survives the merge into the
    # run record (the cross-repo contract seam external consumers read).
    assert orchestrator.result.environment_info["system_prompt_semantics"] == "append"

    assert isinstance(orchestrator.sandbox, Sandbox)
    assert orchestrator.sandbox.sandbox_dir is not None
    assert not orchestrator.sandbox.is_persistent
    sandbox_dir = orchestrator.sandbox.sandbox_dir.resolve()
    assert run_dir.resolve() not in [sandbox_dir, *sandbox_dir.parents]

    await orchestrator._cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pre_populate", "expect_warning"),
    [(True, True), (False, False)],
    ids=["non_empty_target_warns", "absent_target_silent"],
)
async def test_direct_write_warns_on_non_empty_target(tmp_path, monkeypatch, caplog, pre_populate, expect_warning):
    """DIRECT_WRITE _setup warns iff the target artifacts dir already exists non-empty."""
    import logging
    from datetime import datetime

    from coder_eval import orchestrator as orchestrator_module
    from coder_eval.models import ApiBackend, DirectRoute, EvaluationResult
    from coder_eval.sandbox import Sandbox

    class DummyAgent:
        async def start(self, working_directory, *, env_path_prepend=None, plugin_tools_dir=None):
            self.working_directory = working_directory

        def get_sdk_options(self):
            return {"env": {"PATH": os.environ.get("PATH", "")}}

        def get_environment_info(self):
            return {}

    async def create_dummy_agent(_self):
        return DummyAgent()

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    if pre_populate:
        target = run_dir / "artifacts" / task.task_id
        target.mkdir(parents=True)
        (target / "stale.txt").write_text("from a prior run")

    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.DIRECT_WRITE, variant_id="test-variant"
    )
    orchestrator.task_file = task_file
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    monkeypatch.setattr(orchestrator_module.settings, "api_backend", ApiBackend.DIRECT)
    monkeypatch.setattr(type(orchestrator_module.settings), "validate_api_keys", lambda _self, _agent_type: None)
    monkeypatch.setattr(orchestrator_module, "resolve_route", lambda _settings: DirectRoute(judge_transport=None))
    monkeypatch.setattr(Orchestrator, "_create_agent", create_dummy_agent)

    with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
        await orchestrator._setup()

    assert isinstance(orchestrator.sandbox, Sandbox)
    warned = any("already exists and is non-empty" in r.message for r in caplog.records)
    assert warned is expect_warning

    await orchestrator._cleanup()


@pytest.mark.asyncio
async def test_orchestrator_cleanup_persistent_sandbox(tmp_path):
    """DIRECT_WRITE: sandbox already lives in artifacts; _cleanup keeps it in place (no move)."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.DIRECT_WRITE, variant_id="test-variant"
    )

    # Initialize result (normally done in run())
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Set up sandbox with persistent target (simulates what _setup does)
    persist_target = run_dir / "artifacts" / task.task_id
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    orchestrator.sandbox.setup(target_dir=persist_target)

    # Create a test file in sandbox
    (persist_target / "output.txt").write_text("agent output")
    assert orchestrator.sandbox.is_persistent

    # Run cleanup
    await orchestrator._cleanup()

    # Verify: sandbox directory still exists (no deletion)
    assert persist_target.exists()
    assert (persist_target / "output.txt").read_text() == "agent output"

    # Verify: result.sandbox_path is set to the persistent sandbox dir
    assert orchestrator.result.sandbox_path == str(persist_target)


@pytest.mark.asyncio
async def test_orchestrator_cleanup_non_persistent_sandbox_with_preserve(tmp_path):
    """MOVE_ON_WRITE: tempdir sandbox is moved into artifacts on _cleanup."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.MOVE_ON_WRITE, variant_id="test-variant"
    )

    # Initialize result
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Set up sandbox WITHOUT target_dir (non-persistent / legacy path)
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create a test file
    (sandbox_dir / "output.txt").write_text("agent output")
    assert not orchestrator.sandbox.is_persistent

    # Run cleanup
    await orchestrator._cleanup()

    # Verify: sandbox was copied to artifacts dir (legacy path)
    expected_preserve_path = run_dir / "artifacts" / task.task_id
    assert expected_preserve_path.exists()
    assert (expected_preserve_path / "output.txt").read_text() == "agent output"
    assert orchestrator.result.sandbox_path == str(expected_preserve_path)

    # Original temp dir should be cleaned up
    assert not sandbox_dir.exists()


@pytest.mark.asyncio
async def test_orchestrator_cleanup_none_mode_deletes_sandbox(tmp_path):
    """NONE: tempdir sandbox is deleted on _cleanup and sandbox_path is cleared."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.NONE, variant_id="test-variant"
    )
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()
    (sandbox_dir / "output.txt").write_text("agent output")

    await orchestrator._cleanup()

    # Sandbox deleted; no artifacts dir created; sandbox_path nulled.
    assert not sandbox_dir.exists()
    assert not (run_dir / "artifacts").exists()
    assert orchestrator.result.sandbox_path is None


# ==================== get_version_info Tests ====================


def test_get_version_info_without_sandbox_path():
    """Test get_version_info() backward compatibility without sandbox_path."""
    info = get_version_info()

    # Should return standard keys
    assert "claude_code_cli" in info
    assert "uv" in info
    assert "anthropic" in info
    assert "pydantic" in info

    # Should NOT have CLAUDE.md keys
    assert "claude_md_sha256" not in info
    assert "claude_md_size_bytes" not in info


def test_get_version_info_with_sandbox_path_and_claude_md(tmp_path):
    """Test get_version_info() includes CLAUDE.md hash when present."""
    # Create a CLAUDE.md in the sandbox
    claude_md = tmp_path / "CLAUDE.md"
    content = b"# Test CLAUDE.md\nSome instructions here."
    claude_md.write_bytes(content)

    info = get_version_info(sandbox_path=tmp_path)

    # Should have CLAUDE.md hash
    expected_hash = hashlib.sha256(content).hexdigest()
    assert info["claude_md_sha256"] == expected_hash
    assert info["claude_md_size_bytes"] == str(len(content))


def test_get_version_info_with_sandbox_path_no_claude_md(tmp_path):
    """Test get_version_info() omits CLAUDE.md keys when file doesn't exist."""
    info = get_version_info(sandbox_path=tmp_path)

    assert "claude_md_sha256" not in info
    assert "claude_md_size_bytes" not in info


# ==================== Agent Config on EvaluationResult Tests ====================


def test_evaluation_result_agent_config_default():
    """Test that EvaluationResult.agent_config defaults to None."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
    )

    assert result.agent_config is None


def test_evaluation_result_agent_config_set():
    """Test that EvaluationResult.agent_config can be set from AgentConfig."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, parse_agent_config

    config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write"],
        model="claude-sonnet-4-5-20250514",
    )

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
        agent_config=config,
    )

    assert result.agent_config is not None
    assert result.agent_config.permission_mode == "bypassPermissions"
    assert result.agent_config.allowed_tools == ["Read", "Write"]
    assert result.agent_config.model == "claude-sonnet-4-5-20250514"


def test_evaluation_result_serialization_roundtrip_with_agent_config():
    """Test that EvaluationResult with agent_config survives JSON roundtrip."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, parse_agent_config

    config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read"],
        model="claude-sonnet-4-5-20250514",
    )

    original = EvaluationResult(
        task_id="roundtrip_test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2025, 1, 1, 12, 0, 0),
        final_status="SUCCESS",
        iteration_count=1,
        agent_config=config,
    )

    # Serialize and deserialize
    json_str = original.model_dump_json()
    restored = EvaluationResult.model_validate_json(json_str)

    assert restored.agent_config is not None
    assert restored.agent_config.type == AgentKind.CLAUDE_CODE
    assert restored.agent_config.permission_mode == "acceptEdits"
    assert restored.agent_config.allowed_tools == ["Read"]
    assert restored.agent_config.model == "claude-sonnet-4-5-20250514"


def test_evaluation_result_backward_compat_without_agent_config():
    """Test that old JSON without agent_config still deserializes."""
    from coder_eval.models import EvaluationResult

    # JSON from before agent_config existed (no agent_config field)
    old_json = """{
        "task_id": "old_task",
        "task_description": "old test",
        "variant_id": "test-variant",
        "agent_type": "claude-code",
        "started_at": "2025-01-01T12:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1
    }"""

    result = EvaluationResult.model_validate_json(old_json)

    assert result.agent_config is None
    assert result.task_id == "old_task"


# ==================== Batch Error Mapping After Tag Filter Tests ====================


def test_batch_error_mapping_after_tag_filter(tmp_path):
    """Test that batch error results map to correct task file after tag filtering."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _create_error_task_result

    # Simulate: 3 original tasks, filter removes task 0, leaving tasks 1 and 2
    # If task 1 (index 0 in filtered list) errors, it should map to task_b, not task_a

    task_a_result = TaskResult(
        task_id="task_b",
        variant_id="test-variant",
        result=EvaluationResult(
            task_id="task_b",
            task_description="Task B",
            variant_id="test-variant",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
        ),
        duration=10.0,
    )

    task_b_error = _create_error_task_result(
        Path("task_c.yaml"), ValueError("Task C failed"), variant_id="test-variant"
    )

    # Both should have correct task IDs regardless of original ordering
    assert task_a_result.task_id == "task_b"
    assert task_b_error.task_id == "task_c"  # stem of the yaml file


def test_generate_run_summary_includes_agent_config(tmp_path):
    """Test that _generate_run_summary includes agent_config in task results."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult, parse_agent_config
    from coder_eval.orchestration.batch import _generate_run_summary

    config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        model="claude-sonnet-4-5-20250514",
    )

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                agent_config=config,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    # Verify agent_config is included in task results
    assert len(summary.task_results) == 1
    task_result = summary.task_results[0]
    assert task_result["agent_config"] is not None
    assert task_result["agent_config"]["permission_mode"] == "acceptEdits"
    assert task_result["agent_config"]["model"] == "claude-sonnet-4-5-20250514"


def test_generate_run_summary_agent_config_none(tmp_path):
    """Test that _generate_run_summary handles None agent_config."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="ERROR",
                iteration_count=0,
            ),
            duration=0.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.task_results[0]["agent_config"] is None


def test_generate_run_summary_includes_task_path(tmp_path):
    """task_paths kwarg flows into task_results so evalboard can derive skill from path."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
            ),
            duration=10.0,
        ),
        TaskResult(
            task_id="task2",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task2",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
        task_paths={"task1": "tests/tasks/uipath-maestro-flow/smoke/init_validate.yaml"},
    )

    by_id = {t["task_id"]: t for t in summary.task_results}
    assert by_id["task1"]["task_path"] == "tests/tasks/uipath-maestro-flow/smoke/init_validate.yaml"
    # Tasks without a path entry get None — preserves backward-compat for callers
    # that don't pass task_paths at all.
    assert by_id["task2"]["task_path"] is None


def test_generate_run_summary_task_path_omitted(tmp_path):
    """When task_paths kwarg isn't passed, every task_result has task_path=None."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.task_results[0]["task_path"] is None


# ==================== SDK Options Dump Tests ====================


def test_dump_sdk_options_basic():
    """Test _dump_sdk_options with a real ClaudeAgentOptions instance."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.utils import dump_dataclass

    options = ClaudeAgentOptions(
        cwd=_TEST_CWD,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write"],
        model="claude-sonnet-4-5-20250514",
    )

    dump = dump_dataclass(options)

    assert isinstance(dump, dict)
    assert dump["cwd"] == _TEST_CWD
    assert dump["permission_mode"] == "bypassPermissions"
    assert dump["allowed_tools"] == ["Read", "Write"]
    assert dump["model"] == "claude-sonnet-4-5-20250514"


def test_dump_sdk_options_excludes_callables():
    """Test that _dump_sdk_options skips callable fields like stderr."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.utils import dump_dataclass

    def my_stderr(line: str) -> None:
        pass

    options = ClaudeAgentOptions(
        cwd=_TEST_CWD,
        stderr=my_stderr,
    )

    dump = dump_dataclass(options)

    # stderr is a callable and should be excluded
    assert "stderr" not in dump


def test_dump_sdk_options_includes_defaults():
    """Test that _dump_sdk_options includes fields with default values."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.utils import dump_dataclass

    options = ClaudeAgentOptions(cwd=_TEST_CWD)

    dump = dump_dataclass(options)

    # Should include fields with default values
    assert "max_turns" in dump
    assert "model" in dump
    assert "thinking" in dump
    assert "effort" in dump
    assert "mcp_servers" in dump


def test_dump_sdk_options_converts_path():
    """Test that _dump_sdk_options converts Path objects to strings."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.utils import dump_dataclass

    test_path = Path(_TEST_CWD)  # round-tripped through SDK options dump
    options = ClaudeAgentOptions(cwd=test_path)

    dump = dump_dataclass(options)

    assert isinstance(dump["cwd"], str)
    assert dump["cwd"] == str(test_path)


def test_dump_sdk_options_handles_nested_dataclasses():
    """Test that _dump_sdk_options recursively serializes nested dataclasses.

    This test verifies that HookMatcher (a dataclass with callable fields)
    and AgentDefinition (a dataclass with string fields) are properly
    handled without crashing Pydantic serialization.
    """
    import json

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import AgentDefinition, HookMatcher

    from coder_eval.utils import dump_dataclass

    async def my_hook(input, output, ctx):
        return {"action": "allow"}

    options = ClaudeAgentOptions(
        cwd=_TEST_CWD,
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[my_hook], timeout=30.0)]},
        agents={"helper": AgentDefinition(description="test agent", prompt="do stuff")},
    )

    dump = dump_dataclass(options)

    # Hooks should be recursively serialized, with callables stripped
    assert "hooks" in dump
    hook_list = dump["hooks"]["PreToolUse"]
    assert len(hook_list) == 1
    assert hook_list[0]["matcher"] == "Bash"
    assert hook_list[0]["timeout"] == 30.0
    # Callable hooks inside HookMatcher should be stripped (empty list)
    assert hook_list[0]["hooks"] == []

    # AgentDefinition should be recursively converted to dict
    assert "agents" in dump
    assert dump["agents"]["helper"]["description"] == "test agent"
    assert dump["agents"]["helper"]["prompt"] == "do stuff"

    # The entire dump must be JSON-serializable
    json.dumps(dump)

    # And Pydantic-serializable (the actual serialization path)
    from typing import Any

    from pydantic import BaseModel

    class TestModel(BaseModel):
        sdk_options: dict[str, Any] | None = None

    m = TestModel(sdk_options=dump)
    m.model_dump_json()  # Must not raise


def test_evaluation_result_sdk_options_default():
    """Test that EvaluationResult.sdk_options defaults to None."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
    )

    assert result.sdk_options is None


def test_evaluation_result_serialization_roundtrip_with_sdk_options():
    """Test that EvaluationResult with sdk_options survives JSON roundtrip."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    original = EvaluationResult(
        task_id="roundtrip_sdk",
        task_description="test",
        variant_id="test-variant",
        agent_type="claude-code",
        started_at=datetime(2025, 1, 1, 12, 0, 0),
        final_status="SUCCESS",
        iteration_count=1,
        sdk_options={
            "cwd": _TEST_CWD,
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read"],
            "model": "claude-sonnet-4-5-20250514",
            "max_turns": None,
            "thinking": None,
            "effort": None,
            "mcp_servers": {},
        },
    )

    json_str = original.model_dump_json()
    restored = EvaluationResult.model_validate_json(json_str)

    assert restored.sdk_options is not None
    assert restored.sdk_options["cwd"] == _TEST_CWD
    assert restored.sdk_options["permission_mode"] == "bypassPermissions"
    assert restored.sdk_options["allowed_tools"] == ["Read"]
    assert restored.sdk_options["model"] == "claude-sonnet-4-5-20250514"
    assert restored.sdk_options["max_turns"] is None


def test_evaluation_result_backward_compat_without_sdk_options():
    """Test that old JSON without sdk_options still deserializes."""
    from coder_eval.models import EvaluationResult

    old_json = """{
        "task_id": "old_task",
        "task_description": "old test",
        "variant_id": "test-variant",
        "agent_type": "claude-code",
        "started_at": "2025-01-01T12:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1
    }"""

    result = EvaluationResult.model_validate_json(old_json)

    assert result.sdk_options is None
    assert result.task_id == "old_task"


def test_generate_run_summary_includes_sdk_options(tmp_path):
    """Test that _generate_run_summary includes sdk_options in task results."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    sdk_opts = {
        "cwd": _SANDBOX_CWD,
        "permission_mode": "bypassPermissions",
        "allowed_tools": [],
        "model": "claude-sonnet-4-5-20250514",
        "max_turns": 50,
        "thinking": None,
    }

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                sdk_options=sdk_opts,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.task_results[0]["sdk_options"] is not None
    assert summary.task_results[0]["sdk_options"]["permission_mode"] == "bypassPermissions"
    assert summary.task_results[0]["sdk_options"]["max_turns"] == 50


def test_claude_code_agent_get_sdk_options_before_communicate():
    """Test that get_sdk_options returns None before communicate() is called."""
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
    from coder_eval.models import parse_agent_config

    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))

    assert agent.get_sdk_options() is None


def test_batch_run_config_accepts_overrides():
    """BatchRunConfig accepts a generic `overrides` map."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(
        run_dir=tmp_subdir("run"),
        overrides={
            "agent.model": "claude-sonnet-4-20250514",
            "agent.permission_mode": "bypassPermissions",
            "run_limits.max_turns": 50,
        },
    )
    assert config.overrides["agent.model"] == "claude-sonnet-4-20250514"
    assert config.overrides["run_limits.max_turns"] == 50


def test_batch_run_config_overrides_default_empty():
    """BatchRunConfig.overrides defaults to an empty dict."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(run_dir=tmp_subdir("run"))
    assert config.overrides == {}


@pytest.mark.asyncio
async def test_overrides_apply_agent_model(tmp_path):
    """The override engine applies an agent.model override to a loaded task."""
    from coder_eval.orchestration.overrides import apply_overrides

    task, _ = load_task(Path("tasks/hello_date.yaml"))
    original_model = task.agent.model

    apply_overrides(task, {"agent.model": "override-model"})

    assert task.agent.model == "override-model"
    assert task.agent.model != original_model


@pytest.mark.asyncio
async def test_overrides_apply_permission_mode(tmp_path):
    """The override engine applies an agent.permission_mode override."""
    from coder_eval.orchestration.overrides import apply_overrides

    task, _ = load_task(Path("tasks/hello_date.yaml"))

    apply_overrides(task, {"agent.permission_mode": "bypassPermissions"})

    assert task.agent.permission_mode == "bypassPermissions"


@pytest.mark.asyncio
async def test_overrides_apply_max_turns_field_merge(tmp_path):
    """run_limits.max_turns override field-merges, preserving other run_limits keys."""
    from coder_eval.orchestration.overrides import apply_overrides

    task, _ = load_task(Path("tasks/hello_date.yaml"))
    # hello_date.yaml ships a baseline run_limits.expected_turns; max_turns is
    # the field this test exercises. The override must field-merge on top.
    baseline_expected_turns = task.run_limits.expected_turns if task.run_limits else None
    assert task.run_limits is None or task.run_limits.max_turns is None

    apply_overrides(task, {"run_limits.max_turns": 42})

    assert task.run_limits is not None
    assert task.run_limits.max_turns == 42
    # Field-merge must preserve other run_limits keys from the task YAML.
    assert task.run_limits.expected_turns == baseline_expected_turns


# ==================== Duplicate Task ID Validation Tests ====================


def test_resolve_all_tasks_rejects_duplicate_task_ids(tmp_path):
    """Test that resolve_all_tasks raises ValueError when tasks share the same task_id."""
    from coder_eval.models import ExperimentDefaults, ExperimentDefinition, ExperimentVariant
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import resolve_all_tasks

    # Create two task YAML files with the same task_id
    task_yaml = """\
task_id: duplicate_id
description: A test task
initial_prompt: Do something
agent:
  type: claude-code
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    path: output.txt
    description: Output file must exist
"""
    task_file_a = tmp_path / "task_a.yaml"
    task_file_b = tmp_path / "task_b.yaml"
    task_file_a.write_text(task_yaml)
    task_file_b.write_text(task_yaml)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    config = BatchRunConfig(run_dir=run_dir)
    default_experiment = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}),
        variants=[ExperimentVariant(variant_id="default")],
    )
    experiment = ExperimentDefinition(
        experiment_id="default",
        variants=[ExperimentVariant(variant_id="default")],
    )

    with pytest.raises(ValueError, match="Duplicate task IDs found"):
        resolve_all_tasks(
            task_files=[task_file_a, task_file_b],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )


# --- Evaluation loop: max_turns exhaustion early-break test ---


@pytest.mark.asyncio
async def test_evaluation_loop_breaks_on_max_turns_exhausted(tmp_path):
    """Orchestrator stops iterating when the agent exhausts max_turns without passing criteria."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from coder_eval.models import (
        CriterionResult,
        EvaluationResult,
        SandboxConfig,
        TurnRecord,
    )

    agent_cfg = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=20,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="exhaustion_test",
        description="Test exhaustion",
        initial_prompt="Do something",
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        task_timeout=None,
        reference=None,
    )

    run_dir = tmp_path / "run" / "exhaustion_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.result = EvaluationResult(
        task_id="exhaustion_test",
        task_description="Test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Agent returns a turn record with max_turns_exhausted=True
    exhausted_turn = TurnRecord(
        iteration=1,
        user_input="test prompt",
        agent_output="I ran out of turns",
        duration_seconds=5.0,
        max_turns_exhausted=True,
    )
    mock_agent = AsyncMock()
    mock_agent.communicate = AsyncMock(return_value=exhausted_turn)
    orchestrator.agent = mock_agent

    # Mock sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    # Mock success checker that always fails
    mock_checker = MagicMock()
    mock_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=0.0)]
    )
    orchestrator.success_checker = mock_checker

    with patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None):
        success = await orchestrator._evaluation_loop()

    # Should NOT succeed
    assert success is False
    # Should have stopped after 1 iteration (not all 5)
    assert orchestrator.result.iteration_count == 1
    # Agent communicate should have been called only once
    assert mock_agent.communicate.call_count == 1
    # max_turns_exhausted should be propagated to the result
    assert orchestrator.result.max_turns_exhausted is True


@pytest.mark.asyncio
async def test_evaluation_loop_preserves_partial_on_crash_retry(tmp_path):
    """First agent.communicate raises AgentCrashError with a partial; retry succeeds.

    Locks the orchestrator wiring between `execute_with_retry` and the
    `_preserve_partial_on_failure` callback: the partial record reaches
    `result.iterations` before the successful retry's record, and both share the
    same iteration number (per the agent-side rollback contract).
    """
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from coder_eval.errors import AgentCrashError
    from coder_eval.models import (
        CommandTelemetry,
        CriterionResult,
        EvaluationResult,
        SandboxConfig,
        TurnRecord,
    )

    agent_cfg = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=20,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="crash_retry_test",
        description="Partial-preservation wiring",
        initial_prompt="Do the thing",
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py must exist")],
        task_timeout=None,
        reference=None,
    )

    run_dir = tmp_path / "run" / "crash_retry_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    partial_cmd = CommandTelemetry(
        tool_name="Skill",
        tool_id="skill-1",
        timestamp=datetime.now(),
        parameters={"skill": "my_skill"},
    )
    partial_record = TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="<partial>",
        commands=[partial_cmd],
        duration_seconds=0.1,
        crashed=True,
        crash_reason="mid-turn failure",
    )
    success_record = TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="all done",
        duration_seconds=0.2,
    )

    mock_agent = AsyncMock()

    call_index = [0]

    async def crash_then_succeed_impl(_prompt, **kwargs):
        call_index[0] += 1
        if call_index[0] == 1:
            mock_agent.pending_turn = partial_record
            raise AgentCrashError("mid-turn failure")
        return success_record

    mock_agent.communicate.side_effect = crash_then_succeed_impl
    orchestrator.agent = mock_agent

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    mock_checker = MagicMock()
    mock_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        success = await orchestrator._evaluation_loop()

    assert success is True
    # communicate called twice: once crashing, once clean.
    assert mock_agent.communicate.call_count == 2
    # Both records reach result.iterations: partial first (via callback), then successful (via main flow).
    assert len(orchestrator.result.iterations) == 2
    preserved, clean = orchestrator.result.iterations
    assert preserved.crashed is True
    assert preserved.commands[0].tool_name == "Skill"
    assert clean.crashed is False
    # Orchestrator-visible iteration number matches across both records.
    assert preserved.iteration == clean.iteration == 1
    # The orchestrator stamps the cause of the crash on the partial so the
    # report can show it between attempts. Crash messages are passed through;
    # timeout messages get a normalised "Agent turn timed out after Ns" form.
    assert preserved.crash_reason == "mid-turn failure"


@pytest.mark.asyncio
async def test_evaluation_loop_stamps_timeout_reason_on_partial(tmp_path):
    """TurnTimeoutError partials get a normalised "timed out after Ns" reason
    (regardless of the exception's message), so the report renders a
    consistent transition label."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from coder_eval.errors import TurnTimeoutError
    from coder_eval.models import (
        CriterionResult,
        EvaluationResult,
        SandboxConfig,
        TurnRecord,
    )

    agent_cfg = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=20,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="timeout_reason_test",
        description="timeout-stamping",
        initial_prompt="Do the thing",
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x")],
        task_timeout=None,
        reference=None,
    )

    run_dir = tmp_path / "run" / "timeout_reason_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    partial_record = TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="<partial>",
        crashed=True,
        crash_reason="Agent turn timed out after 600s",
    )

    mock_agent = AsyncMock()

    async def timeout_impl(_prompt, **kwargs):
        mock_agent.pending_turn = partial_record
        raise TurnTimeoutError(600.0, iteration=1)

    mock_agent.communicate.side_effect = timeout_impl
    orchestrator.agent = mock_agent

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    mock_checker = MagicMock()
    mock_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        patch("asyncio.sleep", new_callable=AsyncMock),
        # TurnTimeoutError is non-retryable, so the loop re-raises after the
        # on_attempt_error callback has already stamped + appended the partial.
        # We only care about the side-effect, so suppress the re-raise.
        pytest.raises(TurnTimeoutError),
    ):
        await orchestrator._evaluation_loop()

    # The preserved partial carries the normalised timeout reason. The
    # render layer uses this to label the inter-attempt transition.
    assert any(
        t.crashed and t.crash_reason == "Agent turn timed out after 600s" for t in orchestrator.result.iterations
    )


def test_aggregate_token_usage_includes_crashed_partials(tmp_path):
    """Crashed partials carrying token_usage must contribute to the run total.

    Each API call is independently billed: a partial that emitted a
    ResultMessage before failing was charged for its input + output, and
    the retry's call is charged separately. Filtering partials would
    under-report actual API spend.
    """
    from datetime import datetime

    from coder_eval.models import (
        EvaluationResult,
        SandboxConfig,
        TokenUsage,
        TurnRecord,
    )

    agent_cfg = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=20,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="token_agg_test",
        description="token agg",
        initial_prompt="p",
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x", description="x")],
        task_timeout=None,
        reference=None,
    )

    run_dir = tmp_path / "run" / "token_agg_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
        environment_info={},
    )
    # Edge case: ResultMessage arrived just before the crash, so the partial
    # carries token_usage. The retry then makes a separate billed call.
    orchestrator.result.iterations = [
        TurnRecord(
            iteration=1,
            user_input="p",
            agent_output="<partial>",
            crashed=True,
            token_usage=TokenUsage(uncached_input_tokens=100, output_tokens=20, total_cost_usd=0.01),
        ),
        TurnRecord(
            iteration=1,
            user_input="p",
            agent_output="ok",
            token_usage=TokenUsage(uncached_input_tokens=300, output_tokens=50, total_cost_usd=0.05),
        ),
    ]

    orchestrator._aggregate_token_usage()

    assert orchestrator.result.total_token_usage is not None
    # Both turns counted: partial (100/20/$0.01) + clean (300/50/$0.05).
    assert orchestrator.result.total_token_usage.input_tokens == 400
    assert orchestrator.result.total_token_usage.output_tokens == 70
    assert orchestrator.result.total_token_usage.total_cost_usd == pytest.approx(0.06)


# --- Phase 3: terminal per-task summary line ---


def _bootstrap_finalize_orchestrator(tmp_path, *, final_status, duration=None, score=None, iterations=1):
    """Build an Orchestrator primed to run _finalize_result without running the loop."""
    from datetime import datetime

    from coder_eval.models import (
        EvaluationResult,
        SandboxConfig,
        parse_agent_config,
    )

    task = TaskDefinition(
        task_id="summary_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "summary_run"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task, run_dir, preservation_mode=PreservationMode.NONE, variant_id="v1")
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="v1",
        agent_type=task.agent.type,
        started_at=datetime.now(),
        final_status=final_status,
        iteration_count=iterations,
        environment_info={},
    )
    if duration is not None:
        orchestrator.result.duration_seconds = duration
    if score is not None:
        orchestrator.result.weighted_score = score

    # _finalize_result uses self.agent only for sdk_options; a None agent
    # skips that branch. Patch HTML writer to a no-op so the summary-line
    # test does not depend on the HTML report pipeline.
    orchestrator.agent = None
    return orchestrator


def test_finalize_result_logs_summary_on_success(tmp_path, caplog):
    import logging as _logging
    import time
    from unittest.mock import patch

    from coder_eval.models import FinalStatus

    orch = _bootstrap_finalize_orchestrator(tmp_path, final_status=FinalStatus.SUCCESS, iterations=2)

    with (
        caplog.at_level(_logging.INFO, logger="coder_eval.orchestrator"),
        patch("coder_eval.reports_html.write_task_html", return_value=None),
    ):
        orch._finalize_result(start_time=time.time() - 1.5)

    summary_records = [r for r in caplog.records if "Task finished:" in r.getMessage()]
    assert len(summary_records) == 1
    msg = summary_records[0].getMessage()
    assert "status=SUCCESS" in msg
    assert "iterations=2" in msg
    # Duration is computed from start_time — just check it's non-negative.
    assert "duration=" in msg
    assert "score=" in msg


def test_finalize_result_logs_summary_on_timeout(tmp_path, caplog):
    import logging as _logging
    import time
    from unittest.mock import patch

    from coder_eval.models import FinalStatus

    orch = _bootstrap_finalize_orchestrator(tmp_path, final_status=FinalStatus.TIMEOUT, iterations=0)

    with (
        caplog.at_level(_logging.INFO, logger="coder_eval.orchestrator"),
        patch("coder_eval.reports_html.write_task_html", return_value=None),
    ):
        orch._finalize_result(start_time=time.time())

    summary_records = [r for r in caplog.records if "Task finished:" in r.getMessage()]
    assert len(summary_records) == 1
    assert "status=TIMEOUT" in summary_records[0].getMessage()
    assert "iterations=0" in summary_records[0].getMessage()


def test_finalize_result_logs_zero_score_when_no_criteria(tmp_path, caplog):
    import logging as _logging
    import time
    from unittest.mock import patch

    from coder_eval.models import FinalStatus

    # ERROR + empty criteria: calculate_weighted_score writes 0.0 onto the
    # result, so the summary line ends up with score=0.000 and duration
    # computed from start_time. Exercises the ERROR status path end-to-end.
    orch = _bootstrap_finalize_orchestrator(tmp_path, final_status=FinalStatus.ERROR, iterations=0)
    with (
        caplog.at_level(_logging.INFO, logger="coder_eval.orchestrator"),
        patch("coder_eval.reports_html.write_task_html", return_value=None),
    ):
        orch._finalize_result(start_time=time.time())

    summary_records = [r for r in caplog.records if "Task finished:" in r.getMessage()]
    assert len(summary_records) == 1
    msg = summary_records[0].getMessage()
    assert "score=0.000" in msg
    assert "duration=" in msg


# --- Evaluate-only mode loads reference and forwards it to check_all ---


@pytest.mark.asyncio
async def test_evaluation_loop_evaluate_only_loads_reference(tmp_path):
    """Evaluate-only branch (agent is None) must still stage the reference and
    forward it to SuccessChecker.check_all_async.

    Regression: previously this branch called check_all without the reference,
    so judge-style criteria (llm_judge / agent_judge) silently saw no
    reference even when task.reference was set — surfaced as
    "include_reference=True but reference not set" in the judge_context log.
    """
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    from coder_eval.models import (
        CriterionResult,
        EvaluationResult,
        ReferenceSource,
        SandboxConfig,
    )

    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "solution.txt").write_text("REFERENCE_CONTENT")

    agent_cfg = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=1,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="evaluate_only_ref_test",
        description="evaluate-only with reference",
        initial_prompt="ignored",
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x", description="x")],
        task_timeout=None,
        reference=ReferenceSource(directory="reference"),
    )

    run_dir = tmp_path / "run" / "evaluate_only_ref"
    run_dir.mkdir(parents=True)

    # task_file is what the reference directory path resolves against.
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("# placeholder")

    orchestrator = Orchestrator(
        task=task,
        run_dir=run_dir,
        variant_id="evaluate-only-test",
        task_file=task_yaml,
    )
    orchestrator.result = EvaluationResult(
        task_id="evaluate_only_ref_test",
        task_description="evaluate-only with reference",
        variant_id="evaluate-only-test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )
    # Evaluate-only mode is signalled by orchestrator.agent is None.
    assert orchestrator.agent is None

    mock_checker = MagicMock()
    mock_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    # _setup() normally does this; the test drives _evaluation_loop directly.
    await orchestrator._stage_reference()
    await orchestrator._evaluation_loop()

    mock_checker.check_all_async.assert_called_once()
    kwargs = mock_checker.check_all_async.call_args.kwargs
    staged = kwargs["reference_dir"]
    assert staged is not None
    # A per-run COPY, never the checked-out source — that is what makes the
    # mode-000 window safe to apply under a parallel batch.
    assert staged != ref_dir
    assert (staged / "solution.txt").read_text() == "REFERENCE_CONTENT"
    # turn_records is empty in evaluate-only mode but the kwarg should still be wired.
    assert kwargs["turn_records"] == []


async def test_cleanup_workspace_dir_captures_out(tmp_path):
    """workspace_dir set -> _cleanup copies the in-place workspace to
    run_dir/artifacts/<task_id> and points sandbox_path at the copy."""
    from coder_eval.models import EvaluationResult, FinalStatus, SandboxConfig
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ws = tmp_path / "ws"

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="t", workspace_dir=ws)
    orchestrator.sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id=task.task_id)
    orchestrator.sandbox.setup(target_dir=ws)  # run-in-place at ws
    (ws / "deliverable.txt").write_text("done", encoding="utf-8")
    orchestrator.agent = None  # cleanup guards on None
    assert task.agent is not None
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        agent_type=task.agent.type,
        started_at=0.0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
    )

    await orchestrator._cleanup()

    expected = run_dir / "artifacts" / task.task_id
    assert orchestrator.result.sandbox_path == str(expected)
    assert (expected / "deliverable.txt").read_text(encoding="utf-8") == "done"


async def test_cleanup_workspace_dir_none_uses_move_on_write(tmp_path):
    """workspace_dir=None leaves the MOVE_ON_WRITE path unchanged (regression)."""
    from coder_eval.models import EvaluationResult, FinalStatus, PreservationMode, SandboxConfig
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    orchestrator = Orchestrator(
        task=task, run_dir=run_dir, preservation_mode=PreservationMode.MOVE_ON_WRITE, variant_id="t"
    )
    orchestrator.sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()  # tempdir (MOVE_ON_WRITE)
    (sandbox_dir / "out.txt").write_text("x", encoding="utf-8")
    orchestrator.agent = None
    assert task.agent is not None
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        agent_type=task.agent.type,
        started_at=0.0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
    )

    await orchestrator._cleanup()

    expected = run_dir / "artifacts" / task.task_id
    assert orchestrator.result.sandbox_path == str(expected)
    assert (expected / "out.txt").read_text(encoding="utf-8") == "x"
