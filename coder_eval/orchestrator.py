"""Main orchestrator for coordinating task evaluation."""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from .agent import Agent
from .analysis import calculate_command_statistics
from .config import settings
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
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
    ):
        """Initialize the orchestrator.

        Args:
            task: Task definition to evaluate
            run_dir: Per-task directory within a run (e.g., runs/2025-10-09_15-30-45/hello_date/)
            preserve_sandbox: Whether to preserve sandbox after completion
            task_file: Path to task YAML file (for resolving reference file paths)
        """
        self.task = task
        self.run_dir = run_dir
        self.preserve_sandbox = preserve_sandbox
        self.task_file = task_file

        # Derived paths
        self.report_path = self.run_dir / "report.json"
        # Note: artifacts directory (run_dir/artifacts) is created on-demand during sandbox preservation

        # Snapshot directory (created on-demand if snapshots enabled)
        self.snapshot_base_dir: Path | None = None

        # Components (initialized in run())
        self.sandbox: Sandbox | None = None
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
        with task_log_handler(task_log_path):
            try:
                # Setup components
                await self._setup()

                # Run the main evaluation loop
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
        # Validate API keys
        settings.validate_api_keys(self.task.agent.type.value)

        # Create sandbox with retry logic
        self.sandbox = Sandbox(self.task.sandbox, task_id=self.task.task_id)

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
        self.success_checker = SuccessChecker(self.sandbox)

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
            context={"task_id": self.task.task_id, "component": "agent", "agent_name": self.task.agent.type.value},
        )

    async def _create_agent(self) -> Agent:
        """Create the appropriate agent based on task configuration.

        Returns:
            Agent instance

        Raises:
            ValueError: If agent type is not supported
        """
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
        # Assert that setup has been called
        assert self.agent is not None, "Agent not initialized"
        assert self.success_checker is not None, "Success checker not initialized"
        assert self.result is not None, "Result not initialized"

        current_prompt = self.task.initial_prompt
        iteration = 0
        success = False

        while iteration < self.task.max_iterations and not success:
            iteration += 1
            self.result.iteration_count = iteration

            self.logger.info(f"Starting iteration {iteration}/{self.task.max_iterations}")

            # Communicate with agent (with retry logic)
            self.logger.debug(f"Sending prompt: {current_prompt[:100]}...")

            # Use lambda with default arguments to safely bind variables
            # (without defaults, closure would capture stale references)
            # Local variable for type narrowing in lambda
            agent = self.agent
            turn_record = await execute_with_retry(
                operation=lambda prompt=current_prompt, a=agent: a.communicate(prompt),
                operation_name=f"Agent communication (iteration {iteration})",
                context={
                    "task_id": self.task.task_id,
                    "component": "agent",
                    "agent_name": self.task.agent.type.value,
                },
            )
            self.result.turns.append(turn_record)

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
    ) -> RunSummary:
        """Run multiple tasks in batch with optional parallelism.

        Delegates to orchestration.batch.run_batch() for the actual implementation.
        This method is kept as a class method for backward compatibility.

        Args:
            task_files: List of paths to task YAML files
            config: Batch execution configuration

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
        return await run_batch_impl(task_files, config)
