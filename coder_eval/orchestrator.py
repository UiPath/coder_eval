"""Main orchestrator for coordinating task evaluation."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import Agent
from .analysis import calculate_command_statistics
from .config import settings
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .errors.timeout import TaskTimeoutError, TurnTimeoutError
from .evaluation.checker import SuccessChecker
from .evaluation.reviewer import LLMReviewer
from .models import (
    AgentKind,
    EvaluationResult,
    RunSummary,
    SnapshotMode,
    TaskDefinition,
)
from .orchestration.batch import run_batch as run_batch_impl
from .orchestration.config import BatchRunConfig
from .orchestration.evaluation import create_iteration_snapshot, generate_next_prompt, load_reference_code
from .sandbox import Sandbox
from .streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from .streaming.events import CriteriaCheckEvent, TurnCompleteEvent, TurnStartEvent
from .utils import get_version_info


# Get module logger
logger = logging.getLogger(__name__)


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
    ):
        """Initialize the orchestrator.

        Args:
            task: Task definition to evaluate
            run_dir: Per-task directory within a run (e.g., runs/2025-10-09_15-30-45/hello_date/)
            preserve_sandbox: Whether to preserve sandbox after completion
            task_file: Path to task YAML file (for resolving reference file paths)
            stream_callback: Optional callback for real-time event streaming
            sandbox: Pre-built Sandbox to use directly; if None, creates one from task config and runs the agent
        """
        self.task = task
        self.run_dir = run_dir
        self.preserve_sandbox = preserve_sandbox
        self.task_file = task_file
        self.stream_callback = stream_callback
        self.sandbox = sandbox

        # Derived paths
        self.report_path = self.run_dir / "report.json"
        # Note: artifacts directory (run_dir/artifacts) is created on-demand during sandbox preservation

        # Snapshot directory (created on-demand if snapshots enabled)
        self.snapshot_base_dir: Path | None = None

        # Components (initialized in run())
        self.agent: Agent | None = None
        self.success_checker: SuccessChecker | None = None
        self.llm_reviewer: LLMReviewer | None = None

        # Result tracking
        self.result: EvaluationResult | None = None

        # Reference solution cache (loaded on-demand)
        self._reference_code: str | None = None

        # Create task-specific logger with automatic task_id context
        self.logger = logging.LoggerAdapter(logger, extra={"task_id": task.task_id})

    async def run(self) -> EvaluationResult:
        """Run the complete evaluation.

        Returns:
            Evaluation result with all details

        Raises:
            RuntimeError: If evaluation fails catastrophically
        """
        from .logging_config import task_log_handler

        start_time = time.time()
        started_at = datetime.now()

        # Orchestrator always receives a fully-expanded single-agent task.
        assert self.task.agent is not None, "task.agent must be set; multi-agent tasks must be expanded before running"

        # Initialize result
        self.result = EvaluationResult(
            task_id=self.task.task_id,
            task_description=self.task.description,
            agent_type=self.task.agent.type,
            started_at=started_at,
            final_status="FAILURE",  # Will be updated
            iteration_count=0,
            environment_info=get_version_info(),
        )

        # Calculate task log path
        task_log_path = self.run_dir / "task.log"
        task_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Use context manager for automatic log handler management
        with task_log_handler(task_log_path, task_id=self.task.task_id):
            try:
                # Setup components
                await self._setup()

                # Wrap evaluation loop with task-level timeout (if configured)
                task_timeout = self.task.task_timeout_seconds
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
                    self.result.final_status = "SUCCESS"
                else:
                    self.result.final_status = "FAILURE"

            except asyncio.CancelledError:
                # Re-raise cancellation to allow proper task cancellation
                raise
            except Exception as e:
                # Handle catastrophic errors
                self.result.final_status = "ERROR"
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

                self.logger.error(f"Evaluation failed: {e}", exc_info=True)

            finally:
                # Always cleanup
                await self._cleanup()

                # Finalize result
                if self.result:
                    self.result.completed_at = datetime.now()
                    self.result.duration_seconds = time.time() - start_time

                    # Calculate final weighted score
                    self.result.calculate_weighted_score(self.task.success_criteria)

                    # Calculate command statistics using analysis module
                    if self.result.turns:
                        self.result.command_stats = calculate_command_statistics(self.result.turns)

                    # Resolve model_used from turns (last turn with model wins) or agent config
                    if self.result.turns:
                        for turn in reversed(self.result.turns):
                            if turn.model_used:
                                self.result.model_used = turn.model_used
                                break
                    if not self.result.model_used and self.task.agent.model:
                        self.result.model_used = self.task.agent.model

                    # Aggregate token usage across all turns
                    if self.result.turns:
                        from .models.telemetry import TokenUsage

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

                    # Aggregate assistant turns across all turns
                    if self.result.turns:
                        self.result.total_assistant_turns = sum(t.assistant_turn_count for t in self.result.turns)

                    # Capture SDK options from agent (if supported)
                    if self.agent:
                        self.result.sdk_options = self.agent.get_sdk_options()

                    # Save report to per-task directory
                    self.report_path.parent.mkdir(parents=True, exist_ok=True)
                    self.report_path.write_text(
                        self.result.model_dump_json(indent=2),
                        encoding="utf-8",
                    )

        return self.result

    async def _setup(self) -> None:
        """Set up all components for evaluation.

        Raises:
            RuntimeError: If setup fails
        """
        agent_cfg = self.task.agent
        assert agent_cfg is not None  # guaranteed by run() assertion above
        if self.sandbox is not None:
            # evaluate-only mode: sandbox already set up, skip agent
            assert self.result is not None
            self.result.sandbox_path = str(self.sandbox.sandbox_dir)
            self.success_checker = SuccessChecker(self.sandbox, task_id=self.task.task_id)
            return

        # Validate API keys
        settings.validate_api_keys(agent_cfg.type.value)

        # Create sandbox with retry logic
        task_dir = self.task_file.parent.resolve() if self.task_file else None
        self.sandbox = Sandbox(self.task.sandbox, task_id=self.task.task_id, task_dir=task_dir)

        async def _setup_sandbox():
            assert self.sandbox is not None
            return await asyncio.to_thread(self.sandbox.setup)

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
            self.logger.info(f"Snapshots enabled: mode={self.task.sandbox.snapshots.mode.value}")

        # Create success checker
        self.success_checker = SuccessChecker(self.sandbox, task_id=self.task.task_id)

        # Create LLM reviewer if enabled
        if self.task.llm_reviewer.enabled:
            self.llm_reviewer = LLMReviewer(self.task.llm_reviewer)

        # Create and start agent with retry logic
        self.agent = await self._create_agent()

        async def _start_agent():
            assert self.agent is not None
            await self.agent.start(str(sandbox_dir))

        await execute_with_retry(
            operation=_start_agent,
            operation_name="Agent start",
            context={"task_id": self.task.task_id, "component": "agent", "agent_name": agent_cfg.type.value},
        )

        # Save agent config on result (copy to prevent mutation of shared reference)
        self.result.agent_config = agent_cfg.model_copy(deep=True)

        # Re-capture environment_info with sandbox path (for CLAUDE.md hash)
        self.result.environment_info = get_version_info(
            sandbox_path=Path(self.result.sandbox_path) if self.result.sandbox_path else None,
        )

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
        assert self.task.agent is not None  # guaranteed by run() assertion
        if self.task.agent.type == AgentKind.CLAUDE_CODE:
            from coder_eval.agents.claude_code_agent import ClaudeCodeAgent

            return ClaudeCodeAgent(self.task.agent)
        else:
            raise ValueError(f"Unsupported agent type: {self.task.agent.type}")

    async def _evaluation_loop(self) -> bool:
        """Run the main evaluation loop.

        Returns:
            True if task succeeded, False otherwise
        """
        assert self.success_checker is not None, "Success checker not initialized"
        assert self.result is not None, "Result not initialized"

        if self.agent is None:
            # evaluate-only mode: no agent, single check
            assert self.success_checker is not None
            assert self.result is not None
            unsupported = [c.type for c in self.task.success_criteria if c.requires_agent]
            if unsupported:
                self.logger.warning(
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

        current_prompt = self.task.initial_prompt
        # Working directory context prepended to every prompt (including feedback) since
        # each communicate() call is stateless and the agent loses context between iterations
        assert self.sandbox is not None and self.sandbox.sandbox_dir is not None
        sandbox_dir = self.sandbox.sandbox_dir
        # Bind agent config locally for Pyright narrowing (self.task.agent is guaranteed non-None by run())
        agent_cfg = self.task.agent
        assert agent_cfg is not None
        iteration = 0
        success = False

        while iteration < self.task.max_iterations and not success:
            iteration += 1
            self.result.iteration_count = iteration

            self.logger.info(f"Starting iteration {iteration}/{self.task.max_iterations}")

            # Communicate with agent (with retry logic)
            prompt_with_cwd = f"Your working directory is: {sandbox_dir}\n\n{current_prompt}"
            self.logger.debug(f"Sending prompt: {current_prompt[:100]}...")

            safe_emit(
                self.stream_callback,
                TurnStartEvent(
                    task_id=self.task.task_id,
                    iteration=iteration,
                    max_iterations=self.task.max_iterations,
                    prompt_preview=current_prompt[:100],
                ),
            )

            # Use lambda with default arguments to safely bind variables
            # (without defaults, closure would capture stale references)
            # Local variable for type narrowing in lambda
            agent = self.agent
            turn_timeout = agent_cfg.turn_timeout_seconds

            # Wrap callback to stamp correct task_id on agent-emitted events
            agent_callback: StreamCallback | None = None
            if self.stream_callback is not None:
                agent_callback = TaskScopedCallback(self.stream_callback, self.task.task_id)

            communicate_coro = execute_with_retry(
                operation=lambda prompt=prompt_with_cwd, a=agent, cb=agent_callback: a.communicate(
                    prompt, stream_callback=cb
                ),
                operation_name=f"Agent communication (iteration {iteration})",
                context={
                    "task_id": self.task.task_id,
                    "component": "agent",
                    "agent_name": agent_cfg.type.value,
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
                    task_id=self.task.task_id,
                    iteration=iteration,
                    duration_s=turn_record.duration_seconds or 0.0,
                    command_count=len(turn_record.commands),
                    token_usage_str=str(turn_record.token_usage) if turn_record.token_usage else "",
                ),
            )

            self.logger.debug(f"Agent response received ({len(turn_record.agent_output)} chars)")

            # Create snapshot after this turn (if enabled)
            if self.snapshot_base_dir and self.sandbox:
                await create_iteration_snapshot(
                    sandbox=self.sandbox,
                    snapshot_base_dir=self.snapshot_base_dir,
                    task=self.task,
                    iteration=iteration,
                    turn_record=turn_record,
                    logger=self.logger,
                )

            # Check success criteria (pass reference code for reference_comparison criterion)
            self.logger.debug("Checking success criteria")
            reference_code, self._reference_code = load_reference_code(
                task=self.task,
                task_file=self.task_file,
                cached_reference=self._reference_code,
                logger=self.logger,
            )
            criteria_results = await asyncio.to_thread(
                self.success_checker.check_all,
                self.task.success_criteria,
                reference_code=reference_code,
                turn_records=self.result.turns,
            )
            self.result.success_criteria_results = criteria_results

            # Determine if all criteria passed their thresholds
            all_passed = all(
                result.score >= criterion.pass_threshold
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            )

            passed_count = sum(
                1
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
                if result.score >= criterion.pass_threshold
            )
            total_count = len(criteria_results)

            # Calculate current weighted score for logging
            total_weighted = sum(
                result.score * criterion.weight
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            )
            total_weight = sum(c.weight for c in self.task.success_criteria)
            current_score = total_weighted / total_weight if total_weight > 0 else 0.0

            self.logger.info(
                f"Success criteria: {passed_count}/{total_count} passed, weighted score: {current_score:.3f}"
            )

            criteria_details = [
                f"{criterion.type}: {'PASS' if result.score >= criterion.pass_threshold else 'FAIL'}"
                + f" ({result.score:.2f})"
                for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            ]
            safe_emit(
                self.stream_callback,
                CriteriaCheckEvent(
                    task_id=self.task.task_id,
                    passed=passed_count,
                    total=total_count,
                    weighted_score=current_score,
                    details=criteria_details,
                ),
            )

            if all_passed:
                self.logger.info("All success criteria passed!")
                success = True
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
                    logger=self.logger,
                )

        return success

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        # Stop agent
        if self.agent:
            try:
                await self.agent.stop()
            except Exception as e:
                self.logger.warning(f"Failed to stop agent: {e}")

        # Cleanup sandbox
        if self.sandbox:
            try:
                if self.preserve_sandbox and self.result:
                    # Compute artifacts directory on-demand
                    artifacts_dir = self.run_dir / "artifacts"
                    # Use asyncio.to_thread to prevent blocking event loop
                    preserved_path = await asyncio.to_thread(self.sandbox.preserve_to, artifacts_dir)
                    self.result.sandbox_path = str(preserved_path)
                    self.logger.info(f"Sandbox preserved to: {preserved_path}")

                # Use asyncio.to_thread to prevent blocking event loop
                await asyncio.to_thread(self.sandbox.cleanup, preserve=False)
            except Exception as e:
                self.logger.warning(f"Failed to cleanup sandbox: {e}")

    @classmethod
    async def run_batch(
        cls,
        task_files: list[Path],
        config: BatchRunConfig,
        on_task_complete: Callable[[dict[str, Any]], None] | None = None,
        on_batch_start: Callable[[int], None] | None = None,
        stream_callback_factory: Callable[[str], StreamCallback] | None = None,
    ) -> RunSummary:
        """Run multiple tasks in batch with optional parallelism.

        Delegates to orchestration.batch.run_batch() for the actual implementation.
        This method is kept as a class method for backward compatibility.

        Args:
            task_files: List of paths to task YAML files
            config: Batch execution configuration
            on_task_complete: Optional callback invoked after each task finishes
            on_batch_start: Optional callback invoked with the final task count after filtering

        Returns:
            RunSummary with aggregated results and statistics

        Raises:
            FileNotFoundError: If task files don't exist
            ValueError: If task files are invalid

        Example:
            >>> config = BatchRunConfig(
            ...     run_dir=Path("runs/my-run"),
            ...     max_parallel=3,
            ...     preserve_sandbox=True,
            ... )
            >>> summary = await Orchestrator.run_batch(
            ...     task_files=[Path("task1.yaml"), Path("task2.yaml")],
            ...     config=config,
            ... )
            >>> print(f"Success: {summary.tasks_succeeded}/{summary.tasks_run}")
        """
        return await run_batch_impl(
            task_files,
            config,
            on_task_complete=on_task_complete,
            on_batch_start=on_batch_start,
            stream_callback_factory=stream_callback_factory,
        )
