"""Main orchestrator for coordinating task evaluation."""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .proxy.server import LLMGatewayProxy

from .agent import Agent
from .agents.watchdog import ThreadedWatchdog
from .analysis import calculate_command_statistics
from .config import settings
from .criteria.commands_efficiency import compute_commands_efficiency
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .errors.timeout import TaskTimeoutError, TurnTimeoutError
from .evaluation.checker import SuccessChecker, _short_failure_reason
from .evaluation.reviewer import LLMReviewer
from .evaluation.summaries import summarize_commands
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
    LLMDecision,
    PostRunResult,
    ProxyRoute,
    ResolvedTask,
    RunSummary,
    SimulationTelemetry,
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
from .path_utils import format_task_log_id
from .sandbox import Sandbox
from .simulation import DialogStopReason, UserSimulator, evaluate_stop
from .streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from .streaming.events import CriteriaCheckEvent, CriterionSummary, TurnCompleteEvent, TurnStartEvent
from .utils import get_version_info


# Get module logger
logger = logging.getLogger(__name__)


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    log_fn: Callable[..., None],
    label: str,
    chunks: list[str],
) -> None:
    """Read ``stream`` line-by-line, log each non-empty line via ``log_fn``,
    and accumulate the raw text into ``chunks`` for later capture.

    Used to forward post_run subprocess output to the orchestrator log in
    real time while still preserving it for ``PostRunResult``. If a single
    line exceeds the StreamReader buffer (rare — only for binary-ish or
    malformed output), it is drained as a chunk and logged as a partial.
    """
    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except asyncio.LimitOverrunError as e:
            # Single line larger than the buffer; drain the buffered bytes so
            # readline() can make progress on the next iteration.
            raw = await stream.readexactly(e.consumed)
            text = raw.decode(errors="replace")
            chunks.append(text)
            log_fn("[%s] (partial line, %d bytes)", label, len(raw))
            continue
        if not raw:
            break
        text = raw.decode(errors="replace")
        chunks.append(text)
        line = text.rstrip()
        if line:
            log_fn("[%s] %s", label, line)


def _extract_failure_reason(result: CriterionResult) -> str | None:
    """Streaming-event wrapper around ``_short_failure_reason``.

    Preserves the historical ``None``-for-no-content contract so
    ``CriterionSummary.failure_reason`` stays ``None`` when there's nothing
    to show. The actual reason text is produced by the shared helper so the
    console FAILED log and the streamed event render identical strings.
    """
    if not result.error and not result.details:
        return None
    reason = _short_failure_reason(result)
    return reason if reason != "no details" else None


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
        replicate_index: int = 0,
    ):
        """Initialize the orchestrator.

        Args:
            task: Task definition to evaluate
            run_dir: Per-task directory within a run (e.g., runs/2025-10-09_15-30-45/default/hello_date/00/)
            preserve_sandbox: Whether to preserve sandbox after completion
            task_file: Path to task YAML file (for resolving reference file paths)
            stream_callback: Optional callback for real-time event streaming
            sandbox: Pre-built Sandbox to use directly; if None, creates one from task config and runs the agent
            variant_id: Experiment variant identifier for this task
            source_yaml: Raw YAML text from the task file
            config_lineage: Config lineage dict (dotted-path -> ConfigLineageEntry)
            replicate_index: Zero-indexed trial number (for simulation tasks with n_trials > 1).
                Defaults to 0, which covers single-shot tasks and single-trial simulations.
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
        self.replicate_index = replicate_index

        # Derived paths
        self.report_path = self.run_dir / "task.json"
        self.html_report_path = self.run_dir / "task.html"
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

        # Canonical id shared with run_dir layout, tqdm label, and streaming events.
        self._log_task_id = format_task_log_id(variant_id, task.task_id, replicate_index)

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

                # Enforce task-level timeout via an OS-thread watchdog that
                # SIGKILLs the in-flight CLI subprocess AND cancels this
                # task. The threaded approach is immune to anyio cancel
                # scopes that were silently swallowing asyncio.wait_for
                # cancellations during long rate-limited API calls.
                task_timeout = self.task.task_timeout

                def _kill_agent_subprocess_sync() -> None:
                    if self.agent is not None:
                        # kill_sync is a synchronous SIGKILL-by-PID, safe to
                        # call from a non-asyncio thread.
                        with suppress(Exception):
                            self.agent.kill_sync()

                with ThreadedWatchdog(
                    timeout_seconds=task_timeout,
                    on_timeout=_kill_agent_subprocess_sync,
                    asyncio_task_to_cancel=asyncio.current_task(),
                    label=f"task_timeout ({self.task.task_id})",
                ) as wd:
                    try:
                        success = await self._evaluation_loop()
                    except asyncio.CancelledError:
                        if wd.fired:
                            raise TaskTimeoutError(
                                task_timeout or 0,
                                task_id=self.task.task_id,
                                elapsed_seconds=time.time() - start_time,
                            ) from None
                        raise
                # Belt-and-suspenders: if the loop returned normally but the
                # watchdog fired during post-loop work or the inner coro
                # swallowed the cancel, still classify as TIMEOUT.
                if wd.fired and task_timeout is not None:
                    raise TaskTimeoutError(
                        task_timeout,
                        task_id=self.task.task_id,
                        elapsed_seconds=time.time() - start_time,
                    )

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
                await self._run_post_run_commands()
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

        # Terminal per-task summary line. Emitted before report writes so a
        # write failure cannot swallow the one-line outcome.
        logger.info(
            "Task finished: status=%s duration=%.1fs score=%.3f iterations=%d",
            self.result.final_status.value,
            self.result.duration_seconds or 0.0,
            self.result.weighted_score or 0.0,
            self.result.iteration_count,
        )

        # Persist
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(self.result.model_dump_json(indent=2), encoding="utf-8")

        # Also emit an HTML trace/report alongside task.json. HTML failure must
        # never mask the underlying run outcome — write_task_html logs and
        # returns None on failure.
        from .reports_html import write_task_html

        write_task_html(self.result, self.html_report_path)

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

        # Working directory context prepended to every prompt (including feedback).
        # The agent resumes its session between iterations via session_id.
        assert self.sandbox is not None and self.sandbox.sandbox_dir is not None
        sandbox_dir = self.sandbox.sandbox_dir

        # When a SimulationConfig is present and enabled, replace the
        # criteria-feedback iteration loop with a multi-turn dialog between
        # the agent and an LLM-simulated user. The single-shot loop below is
        # skipped entirely — simulated tasks run exactly one dialog per call.
        if self.task.simulation is not None and self.task.simulation.enabled:
            # initial_prompt is optional in simulation mode — when unset, the
            # simulator produces the opening utterance itself.
            return await self._simulation_dialog_loop(self.task.initial_prompt, sandbox_dir)

        assert self.task.initial_prompt is not None, "initial_prompt must be resolved before orchestration"
        current_prompt = self.task.initial_prompt

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

            # Pass turn_timeout into the agent so it can enforce it via
            # the ThreadedWatchdog that SIGKILLs the CLI subprocess. The
            # agent is the single authoritative enforcer — no orchestrator
            # backstop because the SDK's anyio cancel scopes made
            # cooperative asyncio.wait_for cancellation unreliable.
            turn_record = await execute_with_retry(
                operation=functools.partial(
                    agent.communicate,
                    prompt_with_cwd,
                    stream_callback=agent_callback,
                    timeout=turn_timeout,
                ),
                operation_name=f"Agent communication (iteration {iteration})",
                context={
                    "task_id": self.task.task_id,
                    "component": "agent",
                    "agent_name": self.task.agent.type.value,
                },
            )
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

            self._emit_criteria_event(criteria_results)

            if all_passed:
                # Outcome is already conveyed by the "Success criteria: X/Y
                # passed" INFO above and the terminal "Task finished" summary;
                # keep a DEBUG trace for post-mortem without doubling up.
                logger.debug("All success criteria passed; exiting iteration loop")
                success = True
                break

            # Summarize tool calls for reviewer context and logging
            tool_calls_summary = summarize_commands(turn_record.commands)
            if tool_calls_summary:
                logger.debug("Tool calls for iteration %d:\n%s", iteration, tool_calls_summary)

            # Criteria failed — run the LLM reviewer (if configured) and persist
            # its decision for this iteration to self.result.llm_review.
            decision = await self._review_iteration(turn_record, reference_code, tool_calls_summary)

            # If the agent exhausted its max_turns without completing, stop early —
            # further iterations are unlikely to succeed.
            if turn_record.max_turns_exhausted:
                self.result.max_turns_exhausted = True
                logger.warning(
                    "Agent exhausted max_turns (%s) without passing criteria."
                    + " Stopping evaluation — further iterations unlikely to succeed.",
                    self.task.agent.max_turns,
                )
                break

            # If not at max iterations, build feedback for the next turn.
            if iteration < self.task.max_iterations:
                current_prompt = generate_next_prompt(
                    task=self.task,
                    criteria_results=criteria_results,
                    decision=decision,
                )

        return success

    async def _simulation_dialog_loop(self, initial_prompt: str | None, sandbox_dir: Path) -> bool:
        """Run the task as a multi-turn dialog driven by an LLM user simulator.

        This replaces the criteria-feedback iteration loop for tasks that
        define a ``simulation`` block. One invocation runs exactly one
        dialog trajectory (trial). Parallel trials are handled upstream by
        the batch expander — this method is per-trial.

        Lifecycle:
          1. Obtain the opening user utterance. If the task pinned one via
             ``initial_prompt``, use it verbatim; otherwise ask the simulator
             to produce it from persona + goal (pure-simulation mode).
          2. Send the opening utterance to the agent as turn 1.
          3. After each agent reply, optionally check success criteria.
             Break with ``criteria_passed`` if they pass and
             ``stop_on_criteria_pass`` is set.
          4. Evaluate stop conditions (turn cap, token budget).
          5. Ask the simulator for the next user message. If the simulator
             emits the stop token, break with ``stop_token``.
          6. Loop. On any simulator exception, terminate with ``error``.
          7. After the dialog ends, run a final criteria check unless one
             just happened, and return pass/fail.

        Emits the same streaming events as the single-shot loop
        (``TurnStartEvent``, ``TurnCompleteEvent``, ``CriteriaCheckEvent``)
        so downstream UI renderers work unchanged. Simulator telemetry is
        recorded on ``self.result.simulation``.
        """
        assert self.result is not None
        assert self.task.simulation is not None
        assert self.agent is not None
        assert self.success_checker is not None
        assert self.task.agent is not None
        sim_config = self.task.simulation

        simulator = UserSimulator(
            config=sim_config,
            task_description=self.task.description,
            initial_prompt=initial_prompt,
            route=self.route,
        )
        await simulator.start()

        # stop_reason is left unset until the loop picks a concrete reason;
        # the final assertion before telemetry-write catches any exit path
        # that forgot to set it, instead of silently defaulting.
        stop_reason: DialogStopReason | None = None
        simulator_input_tokens = 0
        simulator_output_tokens = 0
        simulator_failures = 0
        total_tokens_used = 0
        criteria_results: list[CriterionResult] = []
        criteria_checked_this_turn = False
        all_passed = False
        turns_completed = 0

        try:
            # Pure-simulation mode: no pinned opener — ask the simulator to
            # generate turn 1 from empty history before the agent sees anything.
            if initial_prompt is None:
                try:
                    opener = await simulator.next_user_message([])
                except Exception:
                    simulator_failures += 1
                    logger.exception("User simulator failed to generate opening message — aborting dialog")
                    self.result.simulation = SimulationTelemetry(
                        n_trials=sim_config.n_trials,
                        replicate_index=self.replicate_index,
                        stop_reason=DialogStopReason.ERROR.value,
                        simulator_input_tokens=simulator_input_tokens,
                        simulator_output_tokens=simulator_output_tokens,
                        simulator_failures=simulator_failures,
                        total_turns=0,
                    )
                    return False
                simulator_input_tokens += opener.input_tokens or 0
                simulator_output_tokens += opener.output_tokens or 0
                total_tokens_used += (opener.input_tokens or 0) + (opener.output_tokens or 0)
                current_prompt = opener.text

                # Opener carrying the stop token means the simulator judged the
                # task done before any agent turn ran. Record the telemetry and
                # short-circuit — running an agent turn just to learn this after
                # the fact wastes a turn budget.
                if opener.stop_requested:
                    assert stop_reason is None
                    stop_reason = DialogStopReason.STOP_TOKEN
                    self.result.simulation = SimulationTelemetry(
                        n_trials=sim_config.n_trials,
                        replicate_index=self.replicate_index,
                        stop_reason=stop_reason.value,
                        simulator_input_tokens=simulator_input_tokens,
                        simulator_output_tokens=simulator_output_tokens,
                        simulator_failures=simulator_failures,
                        total_turns=0,
                    )
                    return False
            else:
                current_prompt = initial_prompt
            # Parallel history of clean (user, agent) pairs for the simulator.
            # This intentionally excludes the working-directory prefix that gets
            # prepended to agent prompts — the simulator should see the user's
            # actual utterances, not framework wrapping.
            dialog_pairs: list[tuple[str, str]] = []

            check_every_turn = sim_config.check_criteria in ("every_turn", "both")
            turn_timeout = self.task.agent.turn_timeout

            while True:
                turns_completed += 1
                self.result.iteration_count = turns_completed
                logger.info("Simulation turn %s/%s", turns_completed, sim_config.max_turns)

                prompt_with_cwd = f"Your working directory is: {sandbox_dir.resolve()}\n\n{current_prompt}"
                safe_emit(
                    self.stream_callback,
                    TurnStartEvent(
                        task_id=self._log_task_id,
                        iteration=turns_completed,
                        max_iterations=sim_config.max_turns,
                        prompt_preview=current_prompt[:100],
                    ),
                )

                agent = self.agent
                agent_callback: StreamCallback | None = None
                if self.stream_callback is not None:
                    agent_callback = TaskScopedCallback(self.stream_callback, self._log_task_id)

                communicate_coro = execute_with_retry(
                    operation=functools.partial(
                        agent.communicate,
                        prompt_with_cwd,
                        stream_callback=agent_callback,
                        timeout=turn_timeout,
                    ),
                    operation_name=f"Agent communication (sim turn {turns_completed})",
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
                        # wait_for fired before the agent's own watchdog; force-kill
                        # any in-flight subprocess so the SDK can't keep running.
                        with suppress(Exception):
                            await agent.kill()
                        raise TurnTimeoutError(
                            turn_timeout,
                            task_id=self.task.task_id,
                            iteration=turns_completed,
                        ) from None
                else:
                    turn_record = await communicate_coro
                self.result.turns.append(turn_record)
                dialog_pairs.append((current_prompt, turn_record.agent_output or ""))

                safe_emit(
                    self.stream_callback,
                    TurnCompleteEvent(
                        task_id=self._log_task_id,
                        iteration=turns_completed,
                        duration_s=turn_record.duration_seconds or 0.0,
                        command_count=len(turn_record.commands),
                        token_usage_str=str(turn_record.token_usage) if turn_record.token_usage else "",
                    ),
                )
                if turn_record.token_usage is not None:
                    usage = turn_record.token_usage
                    total_tokens_used += (usage.input_tokens or 0) + (usage.output_tokens or 0)

                if self.snapshot_base_dir and self.sandbox:
                    await create_iteration_snapshot(
                        sandbox=self.sandbox,
                        snapshot_base_dir=self.snapshot_base_dir,
                        task=self.task,
                        iteration=turns_completed,
                        turn_record=turn_record,
                    )

                criteria_checked_this_turn = False
                if check_every_turn:
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
                    self.result.calculate_weighted_score(self.task.success_criteria)
                    criteria_checked_this_turn = True
                    all_passed = all(
                        r.score >= c.pass_threshold
                        for r, c in zip(criteria_results, self.task.success_criteria, strict=True)
                    )
                    self._emit_criteria_event(criteria_results)

                stop_decision = evaluate_stop(
                    config=sim_config,
                    turns_completed=turns_completed,
                    total_tokens_used=total_tokens_used,
                    criteria_all_passed=all_passed,
                )
                if stop_decision.stop:
                    assert stop_decision.reason is not None
                    stop_reason = stop_decision.reason
                    break

                if turn_record.max_turns_exhausted:
                    self.result.max_turns_exhausted = True
                    stop_reason = DialogStopReason.MAX_TURNS
                    logger.warning(
                        "Agent exhausted its inner max_turns during simulation turn %s; ending dialog.",
                        turns_completed,
                    )
                    break

                try:
                    sim_result = await simulator.next_user_message(dialog_pairs)
                except Exception:
                    simulator_failures += 1
                    logger.exception("User simulator failed — ending dialog")
                    stop_reason = DialogStopReason.ERROR
                    break

                simulator_input_tokens += sim_result.input_tokens or 0
                simulator_output_tokens += sim_result.output_tokens or 0
                total_tokens_used += (sim_result.input_tokens or 0) + (sim_result.output_tokens or 0)

                if sim_result.stop_requested:
                    stop_reason = DialogStopReason.STOP_TOKEN
                    break

                current_prompt = sim_result.text

            # Final criteria check for end_of_dialog (or both) modes, or when
            # every_turn mode did not run a check for the final turn.
            end_of_dialog_needed = (
                sim_config.check_criteria in ("end_of_dialog", "both") or not criteria_checked_this_turn
            )
            if end_of_dialog_needed:
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
                self.result.calculate_weighted_score(self.task.success_criteria)
                all_passed = all(
                    r.score >= c.pass_threshold
                    for r, c in zip(criteria_results, self.task.success_criteria, strict=True)
                )
                self._emit_criteria_event(criteria_results)

            assert stop_reason is not None, "dialog loop exited without picking a stop_reason"
            self.result.simulation = SimulationTelemetry(
                n_trials=sim_config.n_trials,
                replicate_index=self.replicate_index,
                stop_reason=stop_reason.value,
                simulator_input_tokens=simulator_input_tokens,
                simulator_output_tokens=simulator_output_tokens,
                simulator_failures=simulator_failures,
                total_turns=turns_completed,
            )
            logger.info(
                "Simulation dialog ended: stop_reason=%s turns=%s criteria_passed=%s",
                stop_reason.value,
                turns_completed,
                all_passed,
            )
            return all_passed
        finally:
            # When the dialog bails out via exception (TurnTimeoutError,
            # TaskTimeoutError, etc.) before reaching the explicit telemetry
            # write above, the happy-path write never happens — record partial
            # telemetry here so analytics still see the run. ``stop_reason``
            # being None at this point means "exit was not an in-band stop
            # decision" (i.e., exception-driven), which we classify as ERROR.
            if self.result is not None and self.result.simulation is None:
                self.result.simulation = SimulationTelemetry(
                    n_trials=sim_config.n_trials,
                    replicate_index=self.replicate_index,
                    stop_reason=(stop_reason or DialogStopReason.ERROR).value,
                    simulator_input_tokens=simulator_input_tokens,
                    simulator_output_tokens=simulator_output_tokens,
                    simulator_failures=simulator_failures,
                    total_turns=turns_completed,
                )
            # Always tear down the simulator agent (and its scratch dir) even
            # when the dialog bails out via exception.
            await simulator.stop()

    def _emit_criteria_event(self, criteria_results: list[CriterionResult]) -> None:
        """Emit a CriteriaCheckEvent for the current success-criteria state.

        Extracted so the single-shot loop and the simulation dialog loop
        produce identical streaming output.
        """
        assert self.result is not None
        pairs = list(zip(criteria_results, self.task.success_criteria, strict=True))
        passed_count = sum(1 for r, c in pairs if r.score >= c.pass_threshold)
        total_count = len(pairs)
        current_score = self.result.weighted_score or 0.0
        criteria_details = [
            f"{criterion.type}: {'PASS' if result.score >= criterion.pass_threshold else 'FAIL'}"
            + f" ({result.score:.2f})"
            for result, criterion in pairs
        ]
        criteria_summaries = [
            CriterionSummary(
                criterion_type=criterion.type,
                description=result.description or criterion.description,
                score=result.score,
                passed=result.score >= criterion.pass_threshold,
                failure_reason=_extract_failure_reason(result) if result.score < criterion.pass_threshold else None,
            )
            for result, criterion in pairs
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

    async def _review_iteration(
        self,
        turn_record: TurnRecord,
        reference_code: str | None,
        tool_calls_summary: str | None,
    ) -> LLMDecision | None:
        """Invoke the LLM reviewer for the current iteration.

        Returns the decision (persisting it to ``self.result.llm_review``) or
        None if no reviewer is configured or the call fails. Failures are
        logged and swallowed so they never mask the run outcome.
        """
        if self.llm_reviewer is None or self.result is None:
            return None

        logger.info("Requesting LLM review")
        reviewer = self.llm_reviewer

        async def _review_operation() -> LLMDecision | None:
            return await asyncio.to_thread(
                reviewer.review,
                task_description=self.task.description,
                agent_output=turn_record.agent_output or "",
                current_iteration=turn_record.iteration,
                max_iterations=self.task.max_iterations,
                reference_solution=reference_code,
                tool_calls_summary=tool_calls_summary,
            )

        try:
            decision = await execute_with_retry(
                operation=_review_operation,
                operation_name="LLM reviewer",
                context={"task_id": self.task.task_id, "component": "evaluator"},
            )
        except Exception:
            logger.exception("LLM review failed — continuing without review")
            return None

        if decision is not None:
            self.result.llm_review = decision
            logger.info("LLM review score: %s", decision.score)
        return decision

    _POST_RUN_MAX_OUTPUT = 100_000  # Truncate stdout/stderr to 100KB
    _POST_RUN_STREAM_LIMIT = 262_144  # StreamReader per-line buffer (256KB)

    async def _run_post_run_commands(self) -> None:
        """Execute post-run commands inside the sandbox after evaluation.

        stdout/stderr are streamed line-by-line to the orchestrator logger as
        the command runs (so long-running cleanup scripts show progress in the
        live log) AND captured in ``post_run_results`` for the report.

        Results never affect pass/fail. Errors are logged as warnings and
        captured in the result.
        """
        if not self.task.post_run or not self.sandbox or not self.sandbox.sandbox_dir or not self.result:
            return

        sandbox_dir = self.sandbox.sandbox_dir
        max_out = self._POST_RUN_MAX_OUTPUT

        for post_run in self.task.post_run:
            start = time.time()
            logger.info("Running post-run command: %s", post_run.command)

            try:
                proc = await asyncio.create_subprocess_shell(
                    post_run.command,
                    cwd=str(sandbox_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self._POST_RUN_STREAM_LIMIT,
                )  # nosec B602,B604 - commands come from task YAML, not user input

                stdout_chunks: list[str] = []
                stderr_chunks: list[str] = []

                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            _pump_stream(proc.stdout, logger.info, "post_run stdout", stdout_chunks),
                            _pump_stream(proc.stderr, logger.warning, "post_run stderr", stderr_chunks),
                            proc.wait(),
                        ),
                        timeout=post_run.timeout,
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    self.result.post_run_results.append(
                        PostRunResult(
                            command=post_run.command,
                            stdout="".join(stdout_chunks)[:max_out],
                            stderr="".join(stderr_chunks)[:max_out],
                            error=f"Timed out after {post_run.timeout}s",
                            duration_seconds=time.time() - start,
                        )
                    )
                    logger.warning("Post-run command '%s' timed out after %ds", post_run.command, post_run.timeout)
                    continue

                stdout_text = "".join(stdout_chunks)[:max_out]
                stderr_text = "".join(stderr_chunks)[:max_out]
                self.result.post_run_results.append(
                    PostRunResult(
                        command=post_run.command,
                        exit_code=proc.returncode,
                        stdout=stdout_text,
                        stderr=stderr_text,
                        duration_seconds=time.time() - start,
                    )
                )
                if proc.returncode != 0:
                    logger.warning(
                        "Post-run command '%s' exited with code %d: %s",
                        post_run.command,
                        proc.returncode,
                        stderr_text[:200],
                    )
            except Exception as e:
                self.result.post_run_results.append(
                    PostRunResult(
                        command=post_run.command,
                        error=str(e),
                        duration_seconds=time.time() - start,
                    )
                )
                logger.warning("Post-run command '%s' failed: %s", post_run.command, e)

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
