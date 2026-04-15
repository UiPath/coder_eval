"""Main orchestrator for coordinating task evaluation."""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .proxy.server import LLMGatewayProxy

from .agent import Agent
from .analysis import calculate_command_statistics
from .config import settings
from .criteria.commands_efficiency import compute_commands_efficiency
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .errors.timeout import TaskTimeoutError, TurnTimeoutError
from .evaluation.checker import SuccessChecker
from .evaluation.reviewer import LLMReviewer
from .models import (
    ROUTE_NAMES,
    AgentKind,
    ApiBackend,
    ApiRoute,
    BedrockRoute,
    ConfigLineageEntry,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
    ProxyRoute,
    ResolvedTask,
    RunSummary,
    SnapshotMode,
    TaskConfigRecord,
    TaskDefinition,
    TaskResult,
    TurnRecord,
    proxy_config_from_settings,
    resolve_route,
)
from .orchestration.batch import run_batch as run_batch_impl
from .orchestration.config import BatchRunConfig
from .orchestration.evaluation import create_iteration_snapshot, generate_next_prompt, load_reference_code
from .sandbox import Sandbox
from .streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from .streaming.events import CriteriaCheckEvent, CriterionSummary, TurnCompleteEvent, TurnStartEvent
from .utils import get_version_info


# Get module logger
logger = logging.getLogger(__name__)


def _extract_failure_reason(result: CriterionResult) -> str | None:
    """Extract the failure reason from a criterion result.

    Returns the stderr content (up to 500 chars) when available,
    otherwise the first non-empty line from details.
    """
    if result.error:
        return result.error
    if not result.details:
        return None
    # Prefer stderr — that's where check scripts put the failure message
    for line in result.details.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("stderr:"):
            reason = stripped[len("stderr:") :].strip()
            if reason:
                return reason
    # Fall back to first non-empty line
    for line in result.details.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _summarize_tool_calls(turn_record: TurnRecord) -> str | None:
    """Build a concise summary of the agent's tool calls for the LLM reviewer.

    Returns None if there were no tool calls.
    """
    if not turn_record.commands:
        return None

    lines = []
    for i, cmd in enumerate(turn_record.commands, 1):
        status = cmd.result_status or "unknown"
        # Extract the most useful parameter for each tool type
        detail = ""
        params = cmd.parameters
        if cmd.tool_name == "Bash" and "command" in params:
            detail = f" `{params['command'][:120]}`"
        elif cmd.tool_name in ("Read", "Write", "Edit", "Glob") and "file_path" in params:
            detail = f" {params['file_path']}"
        elif cmd.tool_name == "Grep" and "pattern" in params:
            detail = f" pattern={params['pattern'][:60]}"
        elif cmd.tool_name in ("Task", "Agent"):
            detail = f" ({params.get('description', '')[:60]})"

        result_preview = ""
        if cmd.result_summary:
            result_preview = f" → {cmd.result_summary[:80]}"

        lines.append(f"  {i}. [{status}] {cmd.tool_name}{detail}{result_preview}")

    return "\n".join(lines)


class Orchestrator:
    """Coordinates the full evaluation loop for a task.

    Manages the sandbox, agent, and evaluators to run a complete
    task evaluation with multiple iterations.
    """

    def __init__(
        self,
        task: TaskDefinition,
        run_dir: Path,
        preserve_sandbox: bool = False,
        task_file: Path | None = None,
        stream_callback: StreamCallback | None = None,
        sandbox: Sandbox | None = None,
        *,
        variant_id: str,
        source_yaml: str = "",
        config_lineage: dict[str, ConfigLineageEntry] | None = None,
    ):
        """Initialize the orchestrator.

        Args:
            task: Task definition to evaluate
            run_dir: Per-task directory within a run (e.g., runs/2025-10-09_15-30-45/hello_date/)
            preserve_sandbox: Whether to preserve sandbox after completion
            task_file: Path to task YAML file (for resolving reference file paths)
            stream_callback: Optional callback for real-time event streaming
            sandbox: Pre-built Sandbox to use directly; if None, creates one from task config and runs the agent
            variant_id: Experiment variant identifier for this task
            source_yaml: Raw YAML text from the task file
            config_lineage: Config lineage dict (dotted-path -> ConfigLineageEntry)
        """
        self.task = task
        self.run_dir = run_dir
        self.preserve_sandbox = preserve_sandbox
        self.task_file = task_file
        self.stream_callback = stream_callback
        self.sandbox = sandbox
        self.variant_id = variant_id
        self.source_yaml = source_yaml
        self.config_lineage = config_lineage or {}

        # Derived paths
        self.report_path = self.run_dir / "task.json"
        # Note: artifacts directory (run_dir/artifacts) is created on-demand during sandbox preservation

        # Snapshot directory (created on-demand if snapshots enabled)
        self.snapshot_base_dir: Path | None = None

        # Components (initialized in run())
        self.agent: Agent | None = None
        self.success_checker: SuccessChecker | None = None
        self.llm_reviewer: LLMReviewer | None = None

        # Proxy (initialized in _setup if enabled)
        self.proxy: LLMGatewayProxy | None = None

        # API routing (initialized in _setup)
        self.route: ApiRoute | None = None

        # Result tracking
        self.result: EvaluationResult | None = None

        # Reference solution cache (loaded on-demand)
        self._reference_code: str | None = None

        # Task identifier used for log handler context, streaming events, and proxy config
        self._log_task_id = f"{variant_id}/{task.task_id}"

    async def run(self) -> EvaluationResult:
        """Run the complete evaluation.

        Returns:
            Evaluation result with all details

        Raises:
            RuntimeError: If evaluation fails catastrophically
        """
        from .logging_config import task_log_handler

        # Agent must be resolved before reaching the orchestrator
        assert self.task.agent is not None, (
            f"Task '{self.task.task_id}' has no agent config. Ensure experiment resolution ran before orchestration."
        )

        start_time = time.time()
        started_at = datetime.now()

        # Initialize result
        self.result = EvaluationResult(
            task_id=self.task.task_id,
            task_description=self.task.description,
            variant_id=self.variant_id,
            agent_type=self.task.agent.type,
            started_at=started_at,
            final_status=FinalStatus.FAILURE,  # Will be updated
            iteration_count=0,
            environment_info=get_version_info(),
        )

        # Calculate task log path
        task_log_path = self.run_dir / "task.log"
        task_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Use context manager for automatic log handler management
        with task_log_handler(task_log_path, task_id=self._log_task_id):
            try:
                # Setup components
                await self._setup()

                # Wrap evaluation loop with task-level timeout (if configured)
                task_timeout = self.task.task_timeout
                if task_timeout is not None:
                    try:
                        success = await asyncio.wait_for(self._evaluation_loop(), timeout=task_timeout)
                    except TimeoutError:
                        raise TaskTimeoutError(
                            task_timeout,
                            task_id=self.task.task_id,
                            elapsed_seconds=time.time() - start_time,
                        ) from None
                else:
                    success = await self._evaluation_loop()

                # Update final status
                if success:
                    self.result.final_status = FinalStatus.SUCCESS
                elif self.result.max_turns_exhausted:
                    self.result.final_status = FinalStatus.MAX_TURNS_EXHAUSTED
                else:
                    self.result.final_status = FinalStatus.FAILURE

            except asyncio.CancelledError:
                # Re-raise cancellation to allow proper task cancellation
                raise
            except TaskTimeoutError as e:
                # Task-level timeout gets a dedicated status (not generic ERROR)
                self.result.final_status = FinalStatus.TIMEOUT
                self.result.error_message = str(e)

                self.result.error_details = create_error_context(
                    error=e,
                    task_id=self.task.task_id,
                    attempt=max(self.result.iteration_count, 1),
                    component="orchestrator.task_timeout",
                    agent_name=self.task.agent.type.value,
                )

                logger.error(f"Task timed out: {e}")
            except Exception as e:
                # Handle catastrophic errors
                self.result.final_status = FinalStatus.ERROR
                self.result.error_message = str(e)

                # Determine which component failed (setup vs. iteration N)
                if self.result.iteration_count == 0:
                    failed_component = "orchestrator.setup"
                else:
                    failed_component = f"orchestrator.iteration_{self.result.iteration_count}"

                # Capture detailed error context
                self.result.error_details = create_error_context(
                    error=e,
                    task_id=self.task.task_id,
                    attempt=max(self.result.iteration_count, 1),  # Actual iteration attempt (1-indexed)
                    component=failed_component,
                    agent_name=self.task.agent.type.value,
                )

                logger.error(f"Evaluation failed: {e}", exc_info=True)

            finally:
                await self._cleanup()
                self._finalize_result(start_time)

        return self.result

    def _finalize_result(self, start_time: float) -> None:
        """Finalize the evaluation result: scores, telemetry, and persistence."""
        if not self.result:
            return

        if self.task.agent is None:
            logger.error("Cannot finalize result: task.agent is None")
            return

        self.result.completed_at = datetime.now()
        self.result.duration_seconds = time.time() - start_time

        # Weighted score
        self.result.calculate_weighted_score(self.task.success_criteria)

        # Command statistics
        if self.result.turns:
            self.result.command_stats = calculate_command_statistics(self.result.turns)

        # Resolve model_used (last turn with model wins, then agent config)
        if self.result.turns:
            for turn in reversed(self.result.turns):
                if turn.model_used:
                    self.result.model_used = turn.model_used
                    break
        if not self.result.model_used and self.task.agent.model:
            self.result.model_used = self.task.agent.model

        # Aggregate token usage
        self._aggregate_token_usage()

        # Aggregate assistant turns
        if self.result.turns:
            self.result.total_assistant_turns = sum(t.assistant_turn_count for t in self.result.turns)

        # Commands efficiency
        if self.result.turns and self.result.command_stats:
            total_cmds = self.result.command_stats.total_commands
            self.result.actual_commands = total_cmds
            if self.task.expected_commands is not None:
                self.result.expected_commands = self.task.expected_commands
                self.result.commands_efficiency = compute_commands_efficiency(total_cmds, self.task.expected_commands)

        # SDK options snapshot
        if self.agent:
            self.result.sdk_options = self.agent.get_sdk_options()

        # Task config record (warnings=False: discriminated unions produce benign warnings)
        self.result.task_config = TaskConfigRecord(
            resolved=self.task.model_dump(warnings=False),
            source_yaml=self.source_yaml,
            source_file=str(self.task_file) if self.task_file else None,
            lineage=self.config_lineage,
        )

        # Persist
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(self.result.model_dump_json(indent=2), encoding="utf-8")

    def _aggregate_token_usage(self) -> None:
        """Aggregate token usage from turns and proxy, storing on self.result."""
        assert self.result is not None
        from .models.telemetry import TokenUsage

        if self.result.turns:
            usages = [t.token_usage for t in self.result.turns if t.token_usage is not None]
            if usages:
                costs = [u.total_cost_usd for u in usages if u.total_cost_usd is not None]
                self.result.total_token_usage = TokenUsage(
                    input_tokens=sum(u.input_tokens for u in usages),
                    output_tokens=sum(u.output_tokens for u in usages),
                    cache_creation_input_tokens=sum(u.cache_creation_input_tokens for u in usages),
                    cache_read_input_tokens=sum(u.cache_read_input_tokens for u in usages),
                    total_cost_usd=sum(costs) if costs else None,
                )

        # Override with proxy usage when SDK reports zeros
        if self.proxy is not None:
            pu = self.proxy.usage
            sdk_usage = self.result.total_token_usage
            sdk_is_zero = sdk_usage is None or (sdk_usage.input_tokens == 0 and sdk_usage.output_tokens == 0)
            if sdk_is_zero and (pu.input_tokens > 0 or pu.output_tokens > 0):
                self.result.total_token_usage = TokenUsage(
                    input_tokens=pu.input_tokens,
                    output_tokens=pu.output_tokens,
                    cache_creation_input_tokens=pu.cache_creation_input_tokens,
                    cache_read_input_tokens=pu.cache_read_input_tokens,
                    total_cost_usd=self.proxy.get_total_cost(),
                )

    async def _setup(self) -> None:
        """Set up all components for evaluation.

        Raises:
            RuntimeError: If setup fails
        """
        if self.sandbox is not None:
            # evaluate-only mode: sandbox already set up, skip agent
            assert self.result is not None
            self.result.sandbox_path = str(self.sandbox.sandbox_dir)
            self.success_checker = SuccessChecker(self.sandbox)
            return

        # Validate API keys (agent guaranteed non-None after experiment resolution)
        assert self.task.agent is not None
        settings.validate_api_keys(self.task.agent.type.value)

        # Create sandbox with retry logic
        task_dir = self.task_file.parent.resolve() if self.task_file else None
        self.sandbox = Sandbox(self.task.sandbox, task_id=self.task.task_id, task_dir=task_dir)

        # When preserving, work directly in the final artifacts directory (skip copy on cleanup)
        persist_target: Path | None = None
        if self.preserve_sandbox:
            persist_target = self.run_dir / "artifacts" / self.task.task_id

        async def _setup_sandbox() -> Any:
            assert self.sandbox is not None
            return await asyncio.to_thread(self.sandbox.setup, target_dir=persist_target)

        sandbox_dir = await execute_with_retry(
            operation=_setup_sandbox,
            operation_name="Sandbox setup",
            context={"task_id": self.task.task_id, "component": "sandbox"},
        )

        # Assert result is initialized (set in run())
        assert self.result is not None, "Result not initialized"
        self.result.sandbox_path = str(sandbox_dir)

        # Create snapshot directory if snapshots enabled
        if self.task.sandbox.snapshots.mode != SnapshotMode.DISABLED:
            self.snapshot_base_dir = self.run_dir / "snapshots"
            self.snapshot_base_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Snapshots enabled: mode={self.task.sandbox.snapshots.mode.value}")

        # Create success checker
        self.success_checker = SuccessChecker(self.sandbox)

        # Create LLM reviewer if enabled
        if self.task.llm_reviewer.enabled:
            self.llm_reviewer = LLMReviewer(self.task.llm_reviewer)

        # Determine API routing from settings.api_backend enum
        proxy_port: int | None = None
        if settings.api_backend == ApiBackend.PROXY:
            from .proxy import LLMGatewayProxy

            proxy_config = proxy_config_from_settings(settings, task_id=self._log_task_id)
            self.proxy = LLMGatewayProxy(proxy_config)
            await self.proxy.start()
            proxy_port = self.proxy.port
            logger.info("LLM Gateway proxy started on port %d", proxy_port)

        self.route = resolve_route(settings, proxy_port=proxy_port)
        logger.info("API routing: %s", ROUTE_NAMES[type(self.route)])

        # Create and start agent with retry logic
        self.agent = await self._create_agent()

        async def _start_agent() -> None:
            assert self.agent is not None
            await self.agent.start(str(sandbox_dir))

        await execute_with_retry(
            operation=_start_agent,
            operation_name="Agent start",
            context={"task_id": self.task.task_id, "component": "agent", "agent_name": self.task.agent.type.value},
        )

        # Save agent config on result (copy to prevent mutation of shared reference)
        self.result.agent_config = self.task.agent.model_copy(deep=True)

        # Re-capture environment_info with sandbox path (for CLAUDE.md hash)
        self.result.environment_info = get_version_info(
            sandbox_path=Path(self.result.sandbox_path) if self.result.sandbox_path else None,
        )

        # Record API routing mode
        assert self.route is not None
        self.result.environment_info["api_routing"] = ROUTE_NAMES[type(self.route)]
        if isinstance(self.route, BedrockRoute):
            self.result.environment_info["aws_region"] = self.route.region
            if self.route.model:
                self.result.environment_info["bedrock_model"] = self.route.model
        elif isinstance(self.route, ProxyRoute):
            self.result.environment_info["llmgw_url"] = settings.llmgw_url or ""

        # Add installed tool versions (from npm packages etc.)
        if self.sandbox and self.sandbox.installed_tool_versions:
            self.result.environment_info["installed_tools"] = self.sandbox.installed_tool_versions

    async def _create_agent(self) -> Agent:
        """Create the appropriate agent based on task configuration.

        Returns:
            Agent instance

        Raises:
            ValueError: If agent type is not supported
        """
        assert self.task.agent is not None
        if self.task.agent.type == AgentKind.CLAUDE_CODE:
            from coder_eval.agents.claude_code_agent import ClaudeCodeAgent

            assert self.route is not None
            return ClaudeCodeAgent(self.task.agent, route=self.route)
        else:
            raise ValueError(f"Unsupported agent type: {self.task.agent.type}")

    async def _evaluation_loop(self) -> bool:
        """Run the main evaluation loop.

        Returns:
            True if task succeeded, False otherwise
        """
        assert self.success_checker is not None, "Success checker not initialized"
        assert self.result is not None, "Result not initialized"
        assert self.task.agent is not None

        if self.agent is None:
            # evaluate-only mode: no agent, single check
            assert self.success_checker is not None
            assert self.result is not None
            unsupported = [c.type for c in self.task.success_criteria if c.requires_agent]
            if unsupported:
                logger.warning(
                    "Criteria %s require agent execution; results may be incomplete in evaluate-only mode",
                    unsupported,
                )
            self.result.iteration_count = 1
            criteria_results = await asyncio.to_thread(self.success_checker.check_all, self.task.success_criteria)
            self.result.success_criteria_results = criteria_results
            all_passed = all(
                r.score >= c.pass_threshold for r, c in zip(criteria_results, self.task.success_criteria, strict=True)
            )
            return all_passed

        assert self.task.initial_prompt is not None, "initial_prompt must be resolved before orchestration"
        current_prompt = self.task.initial_prompt
        # Working directory context prepended to every prompt (including feedback).
        # The agent resumes its session between iterations via session_id.
        assert self.sandbox is not None and self.sandbox.sandbox_dir is not None
        sandbox_dir = self.sandbox.sandbox_dir
        iteration = 0
        success = False

        while iteration < self.task.max_iterations and not success:
            iteration += 1
            self.result.iteration_count = iteration

            logger.info(f"Starting iteration {iteration}/{self.task.max_iterations}")

            # Communicate with agent (with retry logic)
            prompt_with_cwd = f"Your working directory is: {sandbox_dir.resolve()}\n\n{current_prompt}"
            logger.debug(f"Sending prompt: {current_prompt[:100]}...")

            safe_emit(
                self.stream_callback,
                TurnStartEvent(
                    task_id=self._log_task_id,
                    iteration=iteration,
                    max_iterations=self.task.max_iterations,
                    prompt_preview=current_prompt[:100],
                ),
            )

            agent = self.agent
            turn_timeout = self.task.agent.turn_timeout

            # Wrap callback to stamp correct task_id on agent-emitted events
            agent_callback: StreamCallback | None = None
            if self.stream_callback is not None:
                agent_callback = TaskScopedCallback(self.stream_callback, self._log_task_id)

            communicate_coro = execute_with_retry(
                operation=functools.partial(agent.communicate, prompt_with_cwd, stream_callback=agent_callback),
                operation_name=f"Agent communication (iteration {iteration})",
                context={
                    "task_id": self.task.task_id,
                    "component": "agent",
                    "agent_name": self.task.agent.type.value,
                },
            )

            if turn_timeout is not None:
                try:
                    turn_record = await asyncio.wait_for(communicate_coro, timeout=turn_timeout)
                except TimeoutError:
                    raise TurnTimeoutError(
                        turn_timeout,
                        task_id=self.task.task_id,
                        iteration=iteration,
                    ) from None
            else:
                turn_record = await communicate_coro
            self.result.turns.append(turn_record)

            safe_emit(
                self.stream_callback,
                TurnCompleteEvent(
                    task_id=self._log_task_id,
                    iteration=iteration,
                    duration_s=turn_record.duration_seconds or 0.0,
                    command_count=len(turn_record.commands),
                    token_usage_str=str(turn_record.token_usage) if turn_record.token_usage else "",
                ),
            )

            logger.debug(f"Agent response received ({len(turn_record.agent_output)} chars)")

            # Create snapshot after this turn (if enabled)
            if self.snapshot_base_dir and self.sandbox:
                await create_iteration_snapshot(
                    sandbox=self.sandbox,
                    snapshot_base_dir=self.snapshot_base_dir,
                    task=self.task,
                    iteration=iteration,
                    turn_record=turn_record,
                )

            # Check success criteria (pass reference code for reference_comparison criterion)
            logger.debug("Checking success criteria")
            reference_code, self._reference_code = load_reference_code(
                task=self.task,
                task_file=self.task_file,
                cached_reference=self._reference_code,
            )
            criteria_results = await asyncio.to_thread(
                self.success_checker.check_all,
                self.task.success_criteria,
                reference_code=reference_code,
                turn_records=self.result.turns,
            )
            self.result.success_criteria_results = criteria_results

            # Determine if all criteria passed their thresholds
            pairs = list(zip(criteria_results, self.task.success_criteria, strict=True))
            passed_count = sum(1 for r, c in pairs if r.score >= c.pass_threshold)
            total_count = len(pairs)
            all_passed = passed_count == total_count

            # Reuse the model method for weighted score (single source of truth)
            self.result.calculate_weighted_score(self.task.success_criteria)
            current_score = self.result.weighted_score or 0.0

            logger.info(f"Success criteria: {passed_count}/{total_count} passed, weighted score: {current_score:.3f}")

            criteria_details = [
                f"{criterion.type}: {'PASS' if result.score >= criterion.pass_threshold else 'FAIL'}"
                + f" ({result.score:.2f})"
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            ]
            criteria_summaries = [
                CriterionSummary(
                    criterion_type=criterion.type,
                    description=result.description or criterion.description,
                    score=result.score,
                    passed=result.score >= criterion.pass_threshold,
                    failure_reason=_extract_failure_reason(result) if result.score < criterion.pass_threshold else None,
                )
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            ]
            safe_emit(
                self.stream_callback,
                CriteriaCheckEvent(
                    task_id=self._log_task_id,
                    passed=passed_count,
                    total=total_count,
                    weighted_score=current_score,
                    details=criteria_details,
                    criteria=criteria_summaries,
                ),
            )

            if all_passed:
                logger.info("All success criteria passed!")
                success = True
                break

            # Summarize tool calls for reviewer context and logging
            tool_calls_summary = _summarize_tool_calls(turn_record)
            if tool_calls_summary:
                logger.debug("Tool calls for iteration %d:\n%s", iteration, tool_calls_summary)

            # If the agent exhausted its max_turns without completing, stop early —
            # further iterations are unlikely to succeed.
            if turn_record.max_turns_exhausted:
                self.result.max_turns_exhausted = True
                logger.warning(
                    "Agent exhausted max_turns (%s) without passing criteria. "
                    "Stopping evaluation — further iterations unlikely to succeed.",
                    self.task.agent.max_turns,
                )
                break

            # If not successful and not at max iterations, get feedback
            if iteration < self.task.max_iterations:
                current_prompt = await generate_next_prompt(
                    task=self.task,
                    agent_output=turn_record.agent_output,
                    criteria_results=criteria_results,
                    iteration=iteration,
                    llm_reviewer=self.llm_reviewer,
                    reference_code=reference_code,
                    tool_calls_summary=tool_calls_summary,
                )

        return success

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        # Stop agent
        if self.agent:
            try:
                await self.agent.stop()
            except Exception as e:
                logger.warning(f"Failed to stop agent: {e}")

        # Stop proxy
        if self.proxy:
            try:
                await self.proxy.stop()
            except Exception as e:
                logger.warning(f"Failed to stop proxy: {e}")

        # Cleanup sandbox
        if self.sandbox:
            try:
                if self.preserve_sandbox and self.result:
                    if not self.sandbox.is_persistent:
                        # Sandbox is in a temp dir — copy to artifacts (legacy path)
                        artifacts_dir = self.run_dir / "artifacts"
                        preserved_path = await asyncio.to_thread(self.sandbox.preserve_to, artifacts_dir)
                        self.result.sandbox_path = str(preserved_path)
                        logger.info(f"Sandbox preserved to: {preserved_path}")
                    else:
                        # Sandbox already lives in the artifacts directory — no copy needed
                        self.result.sandbox_path = str(self.sandbox.sandbox_dir)
                        logger.info(f"Sandbox preserved (in-place): {self.sandbox.sandbox_dir}")
                elif self.result:
                    # Sandbox will be deleted; clear stale tempdir path
                    self.result.sandbox_path = None

                # cleanup() is a no-op for persistent dirs (is_persistent=True)
                await asyncio.to_thread(self.sandbox.cleanup, preserve=False)
            except Exception as e:
                logger.warning(f"Failed to cleanup sandbox: {e}")

    @classmethod
    async def run_batch(
        cls,
        resolved_tasks: list[ResolvedTask],
        config: BatchRunConfig,
        on_task_complete: Callable[[TaskResult], None] | None = None,
        on_batch_start: Callable[[int], None] | None = None,
        stream_callback_factory: Callable[[str], StreamCallback] | None = None,
    ) -> tuple[RunSummary, list[TaskResult]]:
        """Run resolved tasks in batch with optional parallelism.

        Delegates to orchestration.batch.run_batch() for the actual implementation.

        Args:
            resolved_tasks: List of fully-resolved tasks from resolve_all_tasks.
            config: Batch execution configuration.
            on_task_complete: Optional callback invoked after each task finishes.
            on_batch_start: Optional callback invoked with the final task count.

        Returns:
            Tuple of (RunSummary, list[TaskResult]).
        """
        return await run_batch_impl(
            resolved_tasks,
            config,
            on_task_complete=on_task_complete,
            on_batch_start=on_batch_start,
            stream_callback_factory=stream_callback_factory,
        )
