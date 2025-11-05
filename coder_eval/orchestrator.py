"""Main orchestrator for coordinating task evaluation."""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .agent import Agent
from .analysis import calculate_command_statistics
from .config import settings
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .evaluator import LLMReviewer, SuccessChecker
from .models import (
    AgentKind,
    CriteriaResults,
    EvaluationResult,
    RunSummary,
    SnapshotMode,
    TaskDefinition,
    TemplateDirSource,
)
from .sandbox import Sandbox
from .utils import get_version_info


# Get module logger
logger = logging.getLogger(__name__)


class BatchRunConfig(BaseModel):
    """Configuration for batch task execution.

    This configuration object encapsulates all parameters needed to run
    multiple tasks in batch mode with optional parallelism.
    """

    run_dir: Path = Field(description="Directory for this batch run")
    max_parallel: int = Field(default=1, ge=1, description="Max concurrent tasks")
    preserve_sandbox: bool = Field(default=False, description="Preserve sandbox after execution")
    max_iterations: int | None = Field(default=None, description="Override max iterations for all tasks")
    snapshot_mode: str | None = Field(default=None, description="Override snapshot mode for all tasks")
    snapshot_checkpoint_freq: int | None = Field(
        default=None, description="Override checkpoint frequency for hybrid mode"
    )


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

            # Use lambda with default argument to safely bind loop variable
            # (without `prompt=current_prompt`, closure would capture stale reference)
            turn_record = await execute_with_retry(
                operation=lambda prompt=current_prompt: self.agent.communicate(prompt),
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
            await self._create_iteration_snapshot(iteration, turn_record)

            # Check success criteria (pass reference code for reference_comparison criterion)
            self.logger.debug("Checking success criteria")
            reference_code = self._load_reference_code()
            criteria_results = await asyncio.to_thread(
                self.success_checker.check_all, self.task.success_criteria, reference_code=reference_code
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
                current_prompt = await self._generate_next_prompt(turn_record.agent_output, criteria_results, iteration)

        return success

    async def _generate_next_prompt(
        self,
        agent_output: str,
        criteria_results: CriteriaResults,
        iteration: int,
    ) -> str:
        """Generate the next prompt based on results and feedback.

        Tries LLM review first if configured. Falls back to deterministic
        feedback listing failed criteria (those with score < pass_threshold).

        Args:
            agent_output: The agent's output from this turn
            criteria_results: Results of success criteria checks
            iteration: Current iteration number

        Returns:
            Next prompt to send to the agent with actionable feedback
        """
        # Assert result is initialized
        assert self.result is not None, "Result not initialized"

        # Try LLM review first if enabled
        if self.llm_reviewer:
            self.logger.info("Requesting LLM review")
            reference_code = self._load_reference_code()

            # Wrap LLM reviewer call with retry logic for network resilience
            async def _review_operation():
                assert self.llm_reviewer is not None
                return await asyncio.to_thread(
                    self.llm_reviewer.review,
                    task_description=self.task.description,
                    agent_output=agent_output,
                    current_iteration=iteration,
                    max_iterations=self.task.max_iterations,
                    reference_solution=reference_code,
                )

            decision = await execute_with_retry(
                operation=_review_operation,
                operation_name="LLM reviewer",
                context={
                    "task_id": self.task.task_id,
                    "component": "evaluator",
                },
            )

            if decision:
                self.result.llm_review = decision
                self.logger.info(f"Issues:\n{decision.issues[:100]}...")
                self.logger.info(f"LLM Score: {decision.score}")

                if decision.next_steps:
                    steps_text = "\n".join(f"- {s}" for s in decision.next_steps)
                    return f"""The task is not yet complete. Here's the feedback:

Issues:
{decision.issues}

Next steps:
{steps_text}

Please address these issues and continue working on the task."""

        # Fallback to deterministic feedback from criteria
        self.logger.info("Using deterministic feedback from failed criteria")

        # Check which criteria failed their pass_threshold
        failed_criteria = [
            (result, criterion)
            for result, criterion in zip(criteria_results, self.task.success_criteria, strict=True)
            if result.score < criterion.pass_threshold
        ]

        if failed_criteria:
            feedback_parts = ["The following checks failed:\n"]
            for result, criterion in failed_criteria:
                feedback_parts.append(f"- {criterion.description}")
                feedback_parts.append(f"  Score: {result.score:.2f} (threshold: {criterion.pass_threshold})")
                if result.error:
                    feedback_parts.append(f"  Error: {result.error}")
                elif result.details:
                    feedback_parts.append(f"  Details: {result.details}")

            feedback_parts.append("\nPlease fix these issues and complete the task.")

            return "\n".join(feedback_parts)

        # Fallback message if no specific feedback
        return "The task is not yet complete. Please continue working on it."

    async def _create_iteration_snapshot(self, iteration: int, turn_record) -> None:
        """Create a snapshot of the sandbox after this iteration.

        Implements hybrid mode: full snapshots at checkpoints, incremental otherwise.
        Gracefully handles errors to prevent snapshot failures from breaking evaluation.

        Args:
            iteration: Current iteration number (1-indexed)
            turn_record: TurnRecord to update with snapshot info
        """
        # Skip if snapshots disabled
        if not self.snapshot_base_dir or not self.sandbox:
            return

        snapshot_config = self.task.sandbox.snapshots
        if snapshot_config.mode == SnapshotMode.DISABLED:
            return

        try:
            # Determine snapshot mode for this iteration
            snapshot_dir = self.snapshot_base_dir / f"iteration_{iteration}"

            # Hybrid mode: full at checkpoints, incremental otherwise
            if snapshot_config.mode == SnapshotMode.HYBRID:
                is_checkpoint = iteration % snapshot_config.checkpoint_frequency == 0
                mode = SnapshotMode.FULL if is_checkpoint else SnapshotMode.INCREMENTAL
            else:
                # Use configured mode directly (FULL or INCREMENTAL)
                mode = snapshot_config.mode

            # Create snapshot
            self.logger.debug(f"Creating {mode.value} snapshot for iteration {iteration}")

            manifest = await self.sandbox.create_snapshot(
                snapshot_dir=snapshot_dir,
                mode=mode,
                changed_files=turn_record.files_changed if mode == SnapshotMode.INCREMENTAL else None,
                ignore_patterns=snapshot_config.ignore_patterns,
            )

            # Update manifest with correct iteration number
            manifest.iteration = iteration

            # Update turn record with snapshot info
            turn_record.snapshot_path = str(snapshot_dir)
            turn_record.snapshot_size_bytes = manifest.size_bytes

            self.logger.info(
                f"Snapshot created: {manifest.file_count} files, {manifest.size_bytes / 1024:.1f} KB, mode={mode.value}"
            )

        except asyncio.CancelledError:
            # Re-raise to allow proper task cancellation
            raise
        except Exception as e:
            # Log error but don't fail the evaluation
            self.logger.warning(f"Failed to create snapshot for iteration {iteration}: {e}")
            # Don't set snapshot_path on turn_record if snapshot failed

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

    def _load_reference_code(self) -> str | None:
        """Load reference code from task definition.

        Returns:
            Reference code content, or None if not defined.

        Raises:
            FileNotFoundError: If reference file path doesn't exist.

        Security: Reference code is NEVER shown to the agent.
        It is only used by LLM reviewer and reference comparison criterion.
        """
        # Return cached if already loaded
        if self._reference_code is not None:
            return self._reference_code

        if not self.task.reference:
            return None

        if self.task.reference.code:
            # Inline code
            self._reference_code = self.task.reference.code
        elif self.task.reference.file:
            # Load from file (resolve relative to task YAML location)
            if not self.task_file:
                raise ValueError("task_file not set, cannot resolve reference file path")
            ref_path = self.task_file.parent / self.task.reference.file
            if not ref_path.exists():
                raise FileNotFoundError(f"Reference file not found: {ref_path} (specified in {self.task_file})")
            self._reference_code = ref_path.read_text()

        # Log that reference was loaded (but NOT the content for security)
        self.logger.info("Reference solution loaded (content hidden for security)")
        return self._reference_code

    @classmethod
    def load_task(cls, task_file: Path) -> TaskDefinition:
        """Load a task definition from a YAML file.

        Args:
            task_file: Path to the task YAML file

        Returns:
            Parsed TaskDefinition

        Raises:
            FileNotFoundError: If task file doesn't exist
            ValueError: If task file is invalid
        """
        if not task_file.exists():
            raise FileNotFoundError(f"Task file not found: {task_file}")

        with open(task_file) as f:
            task_data = yaml.safe_load(f)

        try:
            task = TaskDefinition(**task_data)
            # Resolve relative template paths
            task = cls._resolve_template_paths(task, task_file.parent)
            return task
        except Exception as e:
            raise ValueError(f"Invalid task definition: {e}") from e

    @classmethod
    def _resolve_template_paths(cls, task: TaskDefinition, base_dir: Path) -> TaskDefinition:
        """Resolve relative template paths to absolute paths.

        Mutates TemplateDirSource.path in place for both new API (template_sources)
        and legacy API (template_dir). Other source types don't need path resolution.

        Args:
            task: Task definition with possibly relative paths
            base_dir: Directory containing the task YAML file

        Returns:
            Task with resolved absolute paths (modified in place)
        """
        sandbox_config = task.sandbox

        # Handle new API: iterate template_sources and resolve TemplateDirSource paths
        if sandbox_config.template_sources:
            for source in sandbox_config.template_sources:
                if isinstance(source, TemplateDirSource):
                    template_path = Path(source.path)
                    if not template_path.is_absolute():
                        source.path = str((base_dir / template_path).resolve())
                # Other source types (RepoSource, StarterFilesSource) don't need resolution

        return task

    @classmethod
    async def run_batch(
        cls,
        task_files: list[Path],
        config: BatchRunConfig,
    ) -> RunSummary:
        """Run multiple tasks in batch with optional parallelism.

        This method orchestrates the execution of multiple evaluation tasks,
        managing concurrency, exception handling, and result aggregation.
        Returns a complete RunSummary with all results and statistics.

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
        start_time = datetime.now()

        # Load all tasks first (fail fast if any are invalid)
        tasks: list[tuple[Path, TaskDefinition]] = []
        for task_file in task_files:
            task = cls.load_task(task_file)

            # Apply CLI overrides
            if config.max_iterations:
                task.max_iterations = config.max_iterations

            # Apply snapshot overrides
            if config.snapshot_mode:
                from .models import SnapshotConfig

                # Parse mode string to enum
                mode = SnapshotMode(config.snapshot_mode.lower())

                # Create new snapshot config with overridden values
                task.sandbox.snapshots = SnapshotConfig(
                    mode=mode,
                    checkpoint_frequency=config.snapshot_checkpoint_freq or task.sandbox.snapshots.checkpoint_frequency,
                    ignore_patterns=task.sandbox.snapshots.ignore_patterns,  # Preserve task-specific patterns
                )
            elif config.snapshot_checkpoint_freq:
                # If only checkpoint frequency is overridden, preserve mode
                from .models import SnapshotConfig

                task.sandbox.snapshots = SnapshotConfig(
                    mode=task.sandbox.snapshots.mode,
                    checkpoint_frequency=config.snapshot_checkpoint_freq,
                    ignore_patterns=task.sandbox.snapshots.ignore_patterns,
                )

            tasks.append((task_file, task))

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(config.max_parallel)

        # Create coroutines for all tasks
        async def run_task_with_semaphore(task_file: Path, task: TaskDefinition) -> dict[str, Any]:
            """Run single task with semaphore for concurrency control."""
            async with semaphore:
                return await cls._run_single_task_batch(
                    task_file=task_file,
                    task=task,
                    run_dir=config.run_dir,
                    preserve=config.preserve_sandbox,
                )

        coroutines = [run_task_with_semaphore(task_file, task) for task_file, task in tasks]

        # Execute all tasks (with exception handling)
        results: list[dict[str, Any] | BaseException] = await asyncio.gather(*coroutines, return_exceptions=True)

        # Process results and handle exceptions
        processed_results: list[dict[str, Any]] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                task_file = task_files[i]
                error_result = cls._create_error_result(task_file, result)
                processed_results.append(error_result)
            else:
                processed_results.append(result)

        end_time = datetime.now()

        # Generate and return summary (all-in-one)
        return cls._generate_run_summary(config.run_dir, processed_results, start_time, end_time)

    @classmethod
    async def _run_single_task_batch(
        cls,
        task_file: Path,
        task: TaskDefinition,
        run_dir: Path,
        preserve: bool,
    ) -> dict[str, Any]:
        """Run a single task as part of a batch (internal helper).

        Args:
            task_file: Path to task file (for logging/error reporting)
            task: Loaded task definition
            run_dir: Run-level directory
            preserve: Whether to preserve sandbox

        Returns:
            Dictionary with {task_id, result, duration}
        """
        # Create per-task subdirectory
        task_run_dir = run_dir / task.task_id
        task_run_dir.mkdir(parents=True, exist_ok=True)

        # Create orchestrator for single task
        orchestrator = cls(
            task=task,
            run_dir=task_run_dir,
            preserve_sandbox=preserve,
            task_file=task_file,
        )

        # Run evaluation
        result = await orchestrator.run()

        return {
            "task_id": task.task_id,
            "result": result,
            "duration": result.duration_seconds,
        }

    @classmethod
    def _create_error_result(cls, task_file: Path, error: BaseException) -> dict[str, Any]:
        """Create an error result for a failed task.

        Args:
            task_file: Path to task file that failed
            error: Exception that was raised

        Returns:
            Dictionary with error result in same format as successful results
        """
        error_result = EvaluationResult(
            task_id=task_file.stem,  # Use filename as fallback
            task_description=f"Failed to load task from {task_file}",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="ERROR",
            error_message=str(error),
            iteration_count=0,
            environment_info={},
        )
        return {
            "task_id": error_result.task_id,
            "result": error_result,
            "duration": 0.0,
        }

    @classmethod
    def _generate_run_summary(
        cls,
        run_dir: Path,
        task_results: list[dict[str, Any]],
        start_time: datetime,
        end_time: datetime,
    ) -> RunSummary:
        """Generate run-level summary from batch results.

        Args:
            run_dir: Run directory path
            task_results: List of task result dictionaries
            start_time: Batch start time
            end_time: Batch end time

        Returns:
            RunSummary with aggregated statistics
        """
        from .reports import ReportGenerator

        statuses = [r["result"].final_status for r in task_results]

        summary = RunSummary(
            run_id=run_dir.name,
            start_time=start_time,
            end_time=end_time,
            total_duration_seconds=(end_time - start_time).total_seconds(),
            tasks_run=len(task_results),
            tasks_succeeded=statuses.count("SUCCESS"),
            tasks_failed=statuses.count("FAILURE"),
            tasks_error=statuses.count("ERROR"),
            task_results=[
                {
                    "task_id": r["task_id"],
                    "status": r["result"].final_status,
                    "weighted_score": r["result"].weighted_score,
                    "duration": r["duration"],
                }
                for r in task_results
            ],
            framework_version=get_version_info().get("coder_eval", "unknown"),
            environment_info=get_version_info(),
        )

        # Create run directory if it doesn't exist
        run_dir.mkdir(parents=True, exist_ok=True)

        # Save run-summary.json
        summary_path = run_dir / "run-summary.json"
        summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

        # Generate run-report.md with command statistics
        report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
        report_path = run_dir / "run-report.md"
        report_path.write_text(report_md, encoding="utf-8")

        return summary
