"""Main orchestrator for coordinating task evaluation."""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .proxy.server import LLMGatewayProxy

from .agent import Agent
from .agents.watchdog import ThreadedWatchdog
from .analysis import calculate_command_statistics
from .config import settings
from .criteria.commands_efficiency import compute_commands_efficiency
from .errors import (
    AgentCrashError,
    BudgetExceededError,
    TaskTimeoutError,
    TurnTimeoutError,
)
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .evaluation.checker import SuccessChecker, _short_failure_reason
from .models import (
    ROUTE_NAMES,
    AgentKind,
    ApiBackend,
    ApiRoute,
    BedrockRoute,
    ConfigLineageEntry,
    CriterionResult,
    DirectRoute,
    EvaluationResult,
    FinalStatus,
    PostRunCommand,
    PostRunResult,
    PreRunCommand,
    ProxyRoute,
    ResolvedTask,
    RunSummary,
    SimulationTelemetry,
    TaskConfigRecord,
    TaskDefinition,
    TaskResult,
    TurnRecord,
    UserMessage,
    proxy_config_from_settings,
    resolve_route,
)
from .orchestration.batch import run_batch as run_batch_impl
from .orchestration.config import BatchRunConfig
from .orchestration.evaluation import load_reference
from .path_utils import format_task_log_id, task_log_path
from .sandbox import Sandbox
from .simulation import DialogStopReason, UserSimulator, evaluate_stop
from .streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from .streaming.events import CriteriaCheckEvent, CriterionSummary, TurnCompleteEvent, TurnStartEvent
from .utils import get_version_info


# Get module logger
logger = logging.getLogger(__name__)


# Grace on outer wait_for so the agent's in-band watchdog (which preserves a partial)
# wins the race against the asyncio cancel path (which doesn't).
_WAIT_FOR_GRACE_SECONDS = 2.0


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


# Structural tags emitted by ClaudeCodeAgent._format_messages. Other
# bracketed words (markdown footnotes, pylint codes, unknown SDK message types
# like [TaskStartedMessage]) are intentionally NOT matched — they pass through
# as content. Source of truth for the tag vocabulary is
# ``ClaudeCodeAgent._format_messages``; this regex is telemetry-only (utterance
# extraction for the per-task log), not a correctness-critical parser.
_UTTERANCE_TAG_RE = re.compile(r"^\[(ASSISTANT|RESULT - SUCCESS|RESULT - ERROR|TOOL USE)\](?: (.*))?$")


def _format_routing(route: ApiRoute) -> str:
    """Format the route name for the ``API routing:`` log line.

    For ``DirectRoute`` the resolved judge transport is appended so the choice
    (anthropic / llmgw / none) is visible on every run, not only in the
    persisted ``environment_info`` record.
    """
    name = ROUTE_NAMES[type(route)]
    if isinstance(route, DirectRoute):
        return f"{name} (judge transport: {route.judge_transport or 'none'})"
    return name


def _extract_utterance(raw: str) -> str:
    """Collapse a ClaudeCodeAgent-formatted transcript to a clean utterance.

    Input looks like:
        [ASSISTANT] Sure, I'll do X.
        [TOOL USE] Read
        [ASSISTANT] Here is the answer...
        [RESULT - SUCCESS] Here is the answer...

    The SDK's ``ResultMessage`` duplicates the final assistant text, which
    makes conversation.log read as if every message is repeated. Prefer the
    ``[RESULT - ...]`` payload when it is non-empty (it is the canonical
    final utterance); otherwise fall back to concatenated ``[ASSISTANT]``
    blocks. ``[TOOL USE]`` lines are dropped. Input that does not look
    tagged at all (plain user text like a pinned initial_prompt) is
    returned unchanged.

    Pre-tag content handling: any content appearing BEFORE the first
    tagged line is collected into an implicit ``[ASSISTANT]`` block. It
    survives in the output only on the ASSISTANT-fallback path (no
    ``[RESULT - ...]`` in the transcript). When a ``[RESULT - SUCCESS]``
    is present, it supersedes all ``[ASSISTANT]`` content — including
    any pre-tag content — because the ResultMessage is the SDK's
    canonical final utterance and ASSISTANT lines are chain-of-thought
    that the RESULT already incorporates. ClaudeCodeAgent always begins
    its output with a tag in practice, so this mostly matters for
    defensive handling of upstream format drift.

    Asymmetry note: ``[RESULT - SUCCESS]`` strips its label (it is the
    canonical answer); ``[RESULT - ERROR]`` keeps a ``[RESULT - ERROR]``
    prefix in the output so the error state remains visible in the log.
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    if not any(_UTTERANCE_TAG_RE.match(ln) for ln in lines):
        return raw

    assistant_parts: list[str] = []
    result_parts: list[str] = []
    # Pre-tag content becomes an implicit ASSISTANT block (not dropped).
    current_tag: str = "ASSISTANT"
    current_buf: list[str] = []

    def _flush() -> None:
        text = "\n".join(current_buf).strip()
        if not text:
            return
        if current_tag == "ASSISTANT":
            assistant_parts.append(text)
        elif current_tag == "RESULT - SUCCESS":
            result_parts.append(text)
        elif current_tag == "RESULT - ERROR":
            result_parts.append(f"[RESULT - ERROR] {text}")
        # TOOL USE is dropped.

    for ln in lines:
        match = _UTTERANCE_TAG_RE.match(ln)
        if match:
            _flush()
            current_tag = match.group(1)
            current_buf = [match.group(2) or ""]
        else:
            current_buf.append(ln)
    _flush()

    if result_parts:
        return "\n\n".join(result_parts)
    if assistant_parts:
        return "\n\n".join(assistant_parts)
    return raw


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
        # Clean user<->agent transcript for simulation runs. Written alongside
        # task.log so a human can follow the conversation without the
        # orchestrator noise in between.
        self.conversation_log_path = self.run_dir / "conversation.log"
        # Note: artifacts directory (run_dir/artifacts) is created on-demand during sandbox preservation

        # Components (initialized in run())
        self.agent: Agent | None = None
        self.success_checker: SuccessChecker | None = None

        # Proxy (initialized in _setup if enabled)
        self.proxy: LLMGatewayProxy | None = None

        # API routing (initialized in _setup)
        self.route: ApiRoute | None = None

        # Result tracking
        self.result: EvaluationResult | None = None

        # Reference solution cache (loaded on-demand)
        self._reference_code: str | None = None

        # One-shot flag: emit the "cost budget configured but no cost data" warning
        # exactly once per task even if _check_run_limits fires every turn.
        self._cost_budget_skipped_logged: bool = False

        # One-shot flag: emit the expected_turns rollup warning exactly once per
        # task run even though _check_expected_turns is called after every turn.
        self._expected_turns_warning_emitted: bool = False

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
        assert self.task.agent.type is not None, (
            f"Task '{self.task.task_id}' has no agent.type. Ensure experiment resolution + CLI overrides "
            "ran before orchestration."
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
        task_log_file = task_log_path(self.run_dir)
        task_log_file.parent.mkdir(parents=True, exist_ok=True)  # noqa: CE002 — mkdir on local FS is nanoseconds

        # Use context manager for automatic log handler management
        with task_log_handler(task_log_file, task_id=self._log_task_id) as log_tail:
            try:
                # Setup components
                await self._setup()

                # Run pre-run commands inside the sandbox before the agent starts.
                # A failing command with fail_on_error=True raises RuntimeError,
                # which propagates to the outer except Exception below and lands
                # the run as FinalStatus.ERROR; _run_post_run_commands and
                # _cleanup still execute via the finally block.
                await self._run_pre_run_commands()

                # Enforce task-level timeout via an OS-thread watchdog that
                # SIGKILLs the in-flight CLI subprocess AND cancels this
                # task. The threaded approach is immune to anyio cancel
                # scopes that were silently swallowing asyncio.wait_for
                # cancellations during long rate-limited API calls.
                task_timeout = self.task.run_limits.task_timeout if self.task.run_limits else None

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
            except BudgetExceededError as e:
                # Map token-budget breaches and cost-budget breaches to distinct
                # statuses so per-task records preserve the failure mode.
                if e.budget_name == "usd":
                    self.result.final_status = FinalStatus.COST_BUDGET_EXCEEDED
                    component = "orchestrator.run_limits.cost"
                else:
                    self.result.final_status = FinalStatus.TOKEN_BUDGET_EXCEEDED
                    component = "orchestrator.run_limits.tokens"
                self.result.error_message = str(e)
                self.result.error_details = create_error_context(
                    error=e,
                    task_id=self.task.task_id,
                    attempt=max(self.result.iteration_count, 1),
                    component=component,
                    agent_name=self.task.agent.type.value,
                )
                logger.warning(f"Run limit exceeded: {e}")
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
                # Capture the sanitised log tail AFTER teardown so any errors
                # logged during post-run / cleanup also land in the report,
                # but BEFORE _finalize_result so task.json includes the field.
                # Allowlist non-success terminal statuses; SUCCESS and
                # MAX_TURNS_EXHAUSTED skip the tail to keep task.json compact.
                if self.result.final_status in {
                    FinalStatus.ERROR,
                    FinalStatus.TIMEOUT,
                    FinalStatus.FAILURE,
                    FinalStatus.TOKEN_BUDGET_EXCEEDED,
                    FinalStatus.COST_BUDGET_EXCEEDED,
                }:
                    self.result.error_log_tail = log_tail.get_text() or None
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
        if self.result.iterations:
            self.result.command_stats = calculate_command_statistics(self.result.iterations)

        # Resolve model_used (last turn with model wins, then agent config)
        if self.result.iterations:
            for turn in reversed(self.result.iterations):
                if turn.model_used:
                    self.result.model_used = turn.model_used
                    break
        if not self.result.model_used and self.task.agent.model:
            self.result.model_used = self.task.agent.model

        # Aggregate token usage
        self._aggregate_token_usage()

        # Record whether per-turn cost data was available when a cost budget was set.
        # Lets users audit whether a configured max_usd budget was actually enforceable.
        if self.task.run_limits is not None and self.task.run_limits.max_usd is not None:
            any_cost_reported = any(
                t.token_usage is not None and t.token_usage.total_cost_usd is not None for t in self.result.iterations
            )
            self.result.environment_info["cost_data_available"] = any_cost_reported

        if self.result.iterations:
            self.result.total_assistant_turns = sum(t.assistant_turn_count for t in self.result.iterations)

        # command_stats counts crashed partials too — they're real executed work.
        if self.result.iterations and self.result.command_stats:
            actual_cmds = self.result.command_stats.total_commands
            self.result.actual_commands = actual_cmds
            if self.task.expected_commands is not None:
                self.result.expected_commands = self.task.expected_commands
                self.result.commands_efficiency = compute_commands_efficiency(actual_cmds, self.task.expected_commands)

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
        self.report_path.parent.mkdir(parents=True, exist_ok=True)  # noqa: CE002 — mkdir on local FS is nanoseconds

        # Spill any judge transcripts to sibling judge-<idx>.yaml files BEFORE
        # we dump task.json, so transcript_path is set on each judge result.
        # The inline `transcript` field stays in memory — HTML rendering below
        # uses it directly. We strip it from the JSON dump via `exclude=...`.
        from .evaluation.judge_persistence import spill_judge_transcripts

        spill_judge_transcripts(self.result, self.report_path.parent)

        # Atomic write: tmp file + os.replace. A SIGKILL mid-write (e.g. the
        # docker-driver host-heartbeat watchdog firing) would otherwise leave
        # a truncated task.json that the host parses as malformed-JSON rather
        # than as "no result", conflating two distinct failure modes.
        import os as _os

        report_tmp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        report_tmp.write_text(  # noqa: CE002 — small JSON write at end of run
            self.result.model_dump_json(
                indent=2,
                # Strip inline transcripts: they live in sibling judge-<idx>.yaml
                # next to task.json, referenced by transcript_path. Excluding
                # `transcript` here avoids ~20-100 KB of bloat per judge result
                # in the row record without losing any data.
                exclude={"success_criteria_results": {"__all__": {"transcript"}}},
            ),
            encoding="utf-8",
        )
        _os.replace(report_tmp, self.report_path)

        # Also emit an HTML trace/report alongside task.json. HTML failure must
        # never mask the underlying run outcome — write_task_html logs and
        # returns None on failure.
        from .reports_html import write_task_html

        write_task_html(self.result, self.html_report_path)

    def _check_run_limits(self, *, iteration: int) -> None:
        """Raise BudgetExceededError if any RunLimits budget is exceeded.

        Called after each completed turn. Aggregates across self.result.iterations.
        No-op when self.task.run_limits is None.
        """
        assert self.result is not None
        limits = self.task.run_limits
        if limits is None:
            return

        usages = [t.token_usage for t in self.result.iterations if t.token_usage is not None]
        if not usages:
            return

        input_tokens = sum(u.input_tokens for u in usages)
        if limits.count_cached_input:
            input_tokens += sum(u.cache_read_input_tokens for u in usages)
        output_tokens = sum(u.output_tokens for u in usages)
        total_tokens = input_tokens + output_tokens

        if limits.max_input_tokens is not None and input_tokens > limits.max_input_tokens:
            raise BudgetExceededError(
                "input_tokens",
                actual=input_tokens,
                limit=limits.max_input_tokens,
                task_id=self.task.task_id,
                iteration=iteration,
            )
        if limits.max_output_tokens is not None and output_tokens > limits.max_output_tokens:
            raise BudgetExceededError(
                "output_tokens",
                actual=output_tokens,
                limit=limits.max_output_tokens,
                task_id=self.task.task_id,
                iteration=iteration,
            )
        if limits.max_total_tokens is not None and total_tokens > limits.max_total_tokens:
            raise BudgetExceededError(
                "total_tokens",
                actual=total_tokens,
                limit=limits.max_total_tokens,
                task_id=self.task.task_id,
                iteration=iteration,
            )

        if limits.max_usd is not None:
            costs = [u.total_cost_usd for u in usages if u.total_cost_usd is not None]
            if not costs:
                if not self._cost_budget_skipped_logged:
                    logger.warning(
                        "[%s] max_usd budget configured but no turn reported cost; skipping cost check",
                        self.task.task_id,
                    )
                    self._cost_budget_skipped_logged = True
                return
            total_cost = sum(costs)
            if total_cost > limits.max_usd:
                raise BudgetExceededError(
                    "usd",
                    actual=total_cost,
                    limit=limits.max_usd,
                    task_id=self.task.task_id,
                    iteration=iteration,
                )

    def _check_expected_turns(self, *, iteration: int) -> None:
        """Emit a one-shot warning if visible turns exceed expected_turns.

        Soft sibling of ``_check_run_limits.max_turns``: never aborts the run.
        ``max_turns`` remains the hard cap (enforced inside the SDK). A
        "turn" here is one timeline entry: each tool call plus the final
        reply when present — the same metric evalboard renders. Cumulative
        across iterations so simulation/dialog tasks compare against the
        budget the user set.
        """
        if self.result is None:
            return
        limits = self.task.run_limits
        if limits is None or limits.expected_turns is None:
            return
        if self._expected_turns_warning_emitted:
            return
        from .reports_stats import visible_turn_count

        total = visible_turn_count(self.result)
        if total > limits.expected_turns:
            logger.warning(
                "Visible turns (%d) exceeded expected_turns (%d) at iteration %d "
                "for task %s. Run continues — max_turns remains the hard cap.",
                total,
                limits.expected_turns,
                iteration,
                self.task.task_id,
            )
            self._expected_turns_warning_emitted = True

    def _aggregate_token_usage(self) -> None:
        """Aggregate token usage from turns and proxy, storing on self.result."""
        assert self.result is not None
        from .models.telemetry import TokenUsage

        # Include crashed=True partials: each API call is billed independently.
        if self.result.iterations:
            usages = [t.token_usage for t in self.result.iterations if t.token_usage is not None]
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

            # Start the LLMGW proxy for re-grading workflows that include
            # agent_judge criteria — the judge sub-agent routes through the same
            # proxy as the (already-completed) main coding agent would have.
            # _cleanup handles teardown via `if self.proxy:`.
            if settings.api_backend == ApiBackend.PROXY:
                from .proxy import LLMGatewayProxy

                proxy_config = proxy_config_from_settings(settings, task_id=self._log_task_id)
                self.proxy = LLMGatewayProxy(proxy_config)
                proxy_port = await self.proxy.start()
                self.route = ProxyRoute(port=proxy_port)
                logger.info("LLM Gateway proxy started on port %d (evaluate-only mode)", proxy_port)
            else:
                self.route = resolve_route(settings)
            logger.info("API routing: %s", _format_routing(self.route))
            self.success_checker = SuccessChecker(self.sandbox, route=self.route)
            self._record_route_environment_info()
            return

        # Validate API keys (agent guaranteed non-None after experiment resolution)
        assert self.task.agent is not None
        settings.validate_api_keys(self.task.agent.type.value)

        # Create sandbox with retry logic
        task_dir = self.task_file.parent.resolve() if self.task_file else None
        self.sandbox = Sandbox(self.task.sandbox, task_id=self.task.task_id, task_dir=task_dir)

        # Preserve by copying after execution. Running directly under run_dir/artifacts
        # lets parent-dir node_modules contaminate Node tool resolution on shared hosts.
        async def _setup_sandbox() -> Any:
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

        # Determine API routing from settings.api_backend enum
        if settings.api_backend == ApiBackend.PROXY:
            from .proxy import LLMGatewayProxy

            proxy_config = proxy_config_from_settings(settings, task_id=self._log_task_id)
            self.proxy = LLMGatewayProxy(proxy_config)
            proxy_port = await self.proxy.start()
            self.route = ProxyRoute(port=proxy_port)
            logger.info("LLM Gateway proxy started on port %d", proxy_port)
        else:
            self.route = resolve_route(settings)
        logger.info("API routing: %s", _format_routing(self.route))
        self.success_checker = SuccessChecker(self.sandbox, route=self.route)

        # Create and start agent with retry logic
        self.agent = await self._create_agent()

        env_path_prepend = [str(p) for p in self.sandbox.resolved_mock_path_dirs]
        plugin_tools_dir = self.sandbox.plugin_tools_dir

        async def _start_agent() -> None:
            assert self.agent is not None
            await self.agent.start(
                str(sandbox_dir),
                env_path_prepend=env_path_prepend,
                plugin_tools_dir=plugin_tools_dir,
            )

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

        # Record API routing mode (shared between normal + evaluate-only paths)
        self._record_route_environment_info()

        # Add installed tool versions (from npm packages etc.)
        if self.sandbox and self.sandbox.installed_tool_versions:
            self.result.environment_info["installed_tools"] = self.sandbox.installed_tool_versions

    def _sync_sandbox_command_path_with_agent(self) -> None:
        """Align criteria command PATH with the PATH used for the last agent query.

        Scope: called from the per-turn happy path in ``run_iteration`` /
        ``run_simulation`` *after* a successful ``_communicate_with_retry``.
        That means three pre-existing gaps remain (none introduced by this
        change):

        - **Agent crash / turn timeout** — the sync is skipped because the
          method never returns; criteria fall back to ambient
          ``os.environ['PATH']``. Acceptable: a crashed agent's SDK PATH may
          itself be unreliable.
        - **Evaluate-only mode** (``orchestrator.run_evaluation_only``) — no
          agent turn runs, so no sync. Criteria use ambient PATH, same as
          before this change.
        - **Before the first turn** — same reason; first criterion check
          always runs after at least one turn under the normal flow.

        Sandbox-setup-time sync (using ``SandboxConfig.mock_path_dirs``) was
        considered but rejected: the agent SDK's effective PATH is only
        knowable after the SDK initializes, so a setup-time sync would
        capture only the configured prepends, not the full agent env.

        ``Agent.get_sdk_options()`` is declared synchronous on the ABC
        (``dict[str, Any] | None``). ``AsyncMock``-based test fixtures
        return a coroutine for *any* attribute access regardless of the
        declared signature; ``isawaitable`` plus ``coroutine.close()``
        prevents leaking ``RuntimeWarning: coroutine was never awaited``
        from those fixtures into unrelated tests. That is a test-fixture
        concern, not a production contract violation — logged at DEBUG.
        Returning a non-dict-and-non-None *is* a production contract
        violation and is logged at WARNING.
        """
        if self.agent is None or self.sandbox is None:
            return
        sdk_options = self.agent.get_sdk_options()
        if sdk_options is None:
            return
        if isawaitable(sdk_options):
            close = getattr(sdk_options, "close", None)
            if callable(close):
                # ``coroutine.close()`` only documents ``RuntimeError``
                # (raised when invoked on a currently-running coroutine,
                # which cannot apply here). Narrow the suppress accordingly
                # so genuine unexpected exceptions still propagate.
                with suppress(RuntimeError):
                    close()
            logger.debug(
                "Agent.get_sdk_options() returned an awaitable; skipping PATH sync."
                + " (Typical when tests stub the agent with AsyncMock.)"
            )
            return
        if not isinstance(sdk_options, dict):
            logger.warning(
                "Agent.get_sdk_options() returned non-dict %r; skipping PATH sync.",
                type(sdk_options).__name__,
            )
            return
        sdk_env = sdk_options.get("env")
        if not isinstance(sdk_env, dict):
            return
        path = sdk_env.get("PATH")
        if isinstance(path, str) and path:
            self.sandbox.set_command_base_path(path)

    def _record_route_environment_info(self) -> None:
        """Persist resolved route + judge transport into ``result.environment_info``.

        Called from both the normal and evaluate-only setup paths so audit
        tooling sees identical keys regardless of run mode (catches the
        evaluate-only short-circuit before the recording block).
        """
        assert self.result is not None
        assert self.route is not None
        self.result.environment_info["api_routing"] = ROUTE_NAMES[type(self.route)]
        if isinstance(self.route, BedrockRoute):
            self.result.environment_info["aws_region"] = self.route.region
            if self.route.model:
                self.result.environment_info["bedrock_model"] = self.route.model
        elif isinstance(self.route, ProxyRoute):
            self.result.environment_info["llmgw_url"] = settings.llmgw_url or ""
        elif isinstance(self.route, DirectRoute):
            # Record which transport llm_judge will use under DirectRoute so the
            # choice is visible in run artifacts (and not just the startup log).
            self.result.environment_info["judge_transport"] = self.route.judge_transport or "none"

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

    async def _communicate_with_retry(
        self,
        *,
        prompt: str,
        iteration: int,
        operation_label: str,
    ) -> TurnRecord:
        """Run ``agent.communicate`` with retry, partial-preservation, and a per-attempt timeout.

        Shared by the criteria-feedback and simulation loops. Crashed partials
        from ``AgentCrashError`` / ``TurnTimeoutError`` are appended to
        ``self.result.iterations`` via the ``on_attempt_error`` hook (terminal
        failures included) so observational criteria still see them. Each
        attempt gets a fresh ``turn_timeout``; ``TurnTimeoutError`` is
        ``AGENT_TIMEOUT`` (``max_retries=0``) so it still terminates after
        one attempt.
        """
        assert self.agent is not None
        assert self.task.agent is not None
        assert self.result is not None

        agent = self.agent
        # Local rebind: pyright doesn't carry "is not None" narrowing into closures.
        result = self.result
        run_limits = self.task.run_limits
        turn_timeout = run_limits.turn_timeout if run_limits else None
        max_turns = run_limits.max_turns if run_limits else None

        agent_callback: StreamCallback | None = None
        if self.stream_callback is not None:
            agent_callback = TaskScopedCallback(self.stream_callback, self._log_task_id)

        def _drain_pending_turn(*, attempt: int) -> None:
            """Read agent.pending_turn and, if set, append it to result.iterations."""
            partial = agent.pending_turn
            if partial is not None:
                result.iterations.append(partial)
                logger.debug(
                    "[%s] Drained partial turn record (attempt %d, iteration %d): %d commands",
                    self.task.task_id,
                    attempt + 1,
                    iteration,
                    len(partial.commands),
                )
            else:
                logger.debug(
                    "[%s] No pending_turn to drain on attempt %d (iteration %d)",
                    self.task.task_id,
                    attempt + 1,
                    iteration,
                )

        async def _on_attempt_failure(
            err: Exception,
            attempt: int,
        ) -> None:
            if not isinstance(err, (AgentCrashError, TurnTimeoutError)):
                return
            _drain_pending_turn(attempt=attempt)
            try:
                await agent.discard_pending_turn()
            except Exception:
                logger.warning(
                    "[%s] discard_pending_turn raised on attempt %d",
                    self.task.task_id,
                    attempt + 1,
                    exc_info=True,
                )

        async def _communicate_attempt() -> TurnRecord:
            coro = agent.communicate(
                prompt,
                stream_callback=agent_callback,
                timeout=turn_timeout,
                max_turns=max_turns,
            )
            if turn_timeout is None:
                return await coro
            # Grace buffer: agent's in-band watchdog (sets pending_turn) must beat
            # wait_for cancel so the slot is populated before we give up.
            outer_timeout = turn_timeout + _WAIT_FOR_GRACE_SECONDS
            try:
                return await asyncio.wait_for(coro, timeout=outer_timeout)
            except TimeoutError:
                # Watchdog wedged or too slow. Kill only — drain + discard happen
                # in _on_attempt_failure when this TurnTimeoutError propagates up.
                try:
                    await agent.kill()
                except Exception:
                    logger.warning(
                        "[%s] agent.kill() raised on wait_for backstop path",
                        self.task.task_id,
                        exc_info=True,
                    )
                raise TurnTimeoutError(
                    turn_timeout,
                    task_id=self.task.task_id,
                    iteration=iteration,
                ) from None

        return await execute_with_retry(
            operation=_communicate_attempt,
            operation_name=operation_label,
            context={
                "task_id": self.task.task_id,
                "component": "agent",
                "agent_name": self.task.agent.type.value,
            },
            on_attempt_error=_on_attempt_failure,
        )

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
            # Load reference in evaluate-only mode too: judge criteria with
            # include_reference=true expect this populated even when no agent
            # runs. The agent-driven branch below has the same call.
            reference_code, reference_dir, self._reference_code = load_reference(
                task=self.task,
                task_file=self.task_file,
                cached_reference=self._reference_code,
            )
            criteria_results = await asyncio.to_thread(
                self.success_checker.check_all,
                self.task.success_criteria,
                reference_code=reference_code,
                reference_dir=reference_dir,
                turn_records=self.result.iterations,
            )
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

        iteration = 1
        self.result.iteration_count = iteration

        # Communicate with agent (with retry logic)
        prompt_with_cwd = f"Your working directory is: {sandbox_dir.resolve()}\n\n{current_prompt}"
        logger.debug(f"Sending prompt: {current_prompt[:100]}...")

        safe_emit(
            self.stream_callback,
            TurnStartEvent(
                task_id=self._log_task_id,
                iteration=iteration,
                prompt_preview=current_prompt[:100],
            ),
        )

        turn_record = await self._communicate_with_retry(
            prompt=prompt_with_cwd,
            iteration=iteration,
            operation_label="Agent communication",
        )
        self.result.iterations.append(turn_record)
        self._sync_sandbox_command_path_with_agent()

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

        # Check success criteria (pass reference code for reference_comparison criterion)
        logger.debug("Checking success criteria")
        reference_code, reference_dir, self._reference_code = load_reference(
            task=self.task,
            task_file=self.task_file,
            cached_reference=self._reference_code,
        )
        criteria_results = await asyncio.to_thread(
            self.success_checker.check_all,
            self.task.success_criteria,
            reference_code=reference_code,
            reference_dir=reference_dir,
            turn_records=self.result.iterations,
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

        if turn_record.max_turns_exhausted:
            self.result.max_turns_exhausted = True
            logger.warning(
                "Agent exhausted max_turns (%s) without passing criteria.",
                self.task.run_limits.max_turns if self.task.run_limits else None,
            )

        # Soft cumulative-turn check (logs once; never aborts).
        self._check_expected_turns(iteration=iteration)

        # Budget gate runs AFTER criteria so partial-credit visibility is preserved.
        self._check_run_limits(iteration=iteration)

        return all_passed

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

        # UserMessage captured for the upcoming agent call; prepended to the
        # next turn_record.messages. None outside simulation paths.
        pending_user_turn: UserMessage | None = None
        sim_model_id = getattr(sim_config, "model", None)
        # Track whether we entered the agent-call loop — used by the finally
        # block to decide whether to persist an orphaned pending_user_turn.
        agent_turn_attempted = False

        try:
            # Pure-simulation mode: no pinned opener — ask the simulator to
            # generate turn 1 from empty history before the agent sees anything.
            if initial_prompt is None:
                user_started_wall = datetime.now()
                user_started_mono = time.monotonic()
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
                user_completed_wall = datetime.now()
                user_duration_ms = (time.monotonic() - user_started_mono) * 1000
                simulator_input_tokens += opener.input_tokens or 0
                simulator_output_tokens += opener.output_tokens or 0
                total_tokens_used += (opener.input_tokens or 0) + (opener.output_tokens or 0)
                current_prompt = opener.text
                pending_user_turn = UserMessage(
                    text=opener.text,
                    raw_text=opener.raw_text,
                    stop_requested=opener.stop_requested,
                    started_at=user_started_wall,
                    completed_at=user_completed_wall,
                    generation_duration_ms=user_duration_ms,
                    input_tokens=opener.input_tokens or 0,
                    output_tokens=opener.output_tokens or 0,
                    model=sim_model_id,
                )
                self._log_conversation("USER", 1, current_prompt, metadata="simulator-generated opener")

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
                pending_user_turn = UserMessage(text=initial_prompt)
                self._log_conversation("USER", 1, current_prompt, metadata="pinned initial_prompt")
            # Parallel history of clean (user, agent) pairs for the simulator.
            # This intentionally excludes the working-directory prefix that gets
            # prepended to agent prompts — the simulator should see the user's
            # actual utterances, not framework wrapping.
            dialog_pairs: list[tuple[str, str]] = []

            check_every_turn = sim_config.check_criteria in ("every_turn", "both")

            # turns_completed advances in lockstep with the agent's _iteration:
            # one _communicate_with_retry call per sim turn keeps partials
            # and the successful retry on the same iteration number.
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
                        prompt_preview=current_prompt[:100],
                    ),
                )

                agent_turn_attempted = True
                turn_record = await self._communicate_with_retry(
                    prompt=prompt_with_cwd,
                    iteration=turns_completed,
                    operation_label=f"Agent communication (sim turn {turns_completed})",
                )
                if pending_user_turn is not None:
                    turn_record.messages.insert(0, pending_user_turn)
                    pending_user_turn = None
                self.result.iterations.append(turn_record)
                self._sync_sandbox_command_path_with_agent()
                dialog_pairs.append((current_prompt, _extract_utterance(turn_record.agent_output or "")))
                agent_meta_parts = []
                if turn_record.duration_seconds is not None:
                    agent_meta_parts.append(f"{turn_record.duration_seconds:.1f}s")
                if turn_record.token_usage is not None:
                    usage_parts = []
                    if turn_record.token_usage.input_tokens:
                        usage_parts.append(f"in={turn_record.token_usage.input_tokens}")
                    if turn_record.token_usage.output_tokens:
                        usage_parts.append(f"out={turn_record.token_usage.output_tokens}")
                    if usage_parts:
                        agent_meta_parts.append(" ".join(usage_parts))
                if turn_record.commands:
                    agent_meta_parts.append(f"{len(turn_record.commands)} tool calls")
                self._log_conversation(
                    "AGENT",
                    turns_completed,
                    turn_record.agent_output or "",
                    metadata=", ".join(agent_meta_parts),
                )

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

                criteria_checked_this_turn = False
                if check_every_turn:
                    reference_code, reference_dir, self._reference_code = load_reference(
                        task=self.task,
                        task_file=self.task_file,
                        cached_reference=self._reference_code,
                    )
                    criteria_results = await asyncio.to_thread(
                        self.success_checker.check_all,
                        self.task.success_criteria,
                        reference_code=reference_code,
                        reference_dir=reference_dir,
                        turn_records=self.result.iterations,
                    )
                    self.result.success_criteria_results = criteria_results
                    self.result.calculate_weighted_score(self.task.success_criteria)
                    criteria_checked_this_turn = True
                    all_passed = all(
                        r.score >= c.pass_threshold
                        for r, c in zip(criteria_results, self.task.success_criteria, strict=True)
                    )
                    self._emit_criteria_event(criteria_results)

                # Budget gate: aborts the dialog with a dedicated stop reason and
                # ensures end-of-dialog criteria still run for partial credit.
                try:
                    self._check_run_limits(iteration=turns_completed)
                except BudgetExceededError:
                    stop_reason = DialogStopReason.RUN_LIMIT_EXCEEDED
                    if not criteria_checked_this_turn:
                        reference_code, reference_dir, self._reference_code = load_reference(
                            task=self.task,
                            task_file=self.task_file,
                            cached_reference=self._reference_code,
                        )
                        criteria_results = await asyncio.to_thread(
                            self.success_checker.check_all,
                            self.task.success_criteria,
                            reference_code=reference_code,
                            reference_dir=reference_dir,
                            turn_records=self.result.iterations,
                        )
                        self.result.success_criteria_results = criteria_results
                        self.result.calculate_weighted_score(self.task.success_criteria)
                    raise

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

                # Soft cumulative-turn check (logs once; never aborts the dialog).
                # Runs BEFORE the max_turns break so a single turn that trips both
                # the hard cap and the soft target still emits the expected_turns
                # warning before the dialog terminates.
                self._check_expected_turns(iteration=turns_completed)

                if turn_record.max_turns_exhausted:
                    self.result.max_turns_exhausted = True
                    stop_reason = DialogStopReason.MAX_TURNS
                    logger.warning(
                        "Agent exhausted its inner max_turns during simulation turn %s; ending dialog.",
                        turns_completed,
                    )
                    break

                user_started_wall = datetime.now()
                user_started_mono = time.monotonic()
                try:
                    sim_result = await simulator.next_user_message(dialog_pairs)
                except Exception:
                    simulator_failures += 1
                    logger.exception("User simulator failed — ending dialog")
                    stop_reason = DialogStopReason.ERROR
                    break
                user_completed_wall = datetime.now()
                user_duration_ms = (time.monotonic() - user_started_mono) * 1000

                simulator_input_tokens += sim_result.input_tokens or 0
                simulator_output_tokens += sim_result.output_tokens or 0
                total_tokens_used += (sim_result.input_tokens or 0) + (sim_result.output_tokens or 0)

                pending_user_turn = UserMessage(
                    text=sim_result.text,
                    raw_text=sim_result.raw_text,
                    stop_requested=sim_result.stop_requested,
                    started_at=user_started_wall,
                    completed_at=user_completed_wall,
                    generation_duration_ms=user_duration_ms,
                    input_tokens=sim_result.input_tokens or 0,
                    output_tokens=sim_result.output_tokens or 0,
                    model=sim_model_id,
                )

                if sim_result.stop_requested:
                    self._log_conversation(
                        "USER",
                        turns_completed + 1,
                        sim_result.text,
                        metadata="stop token — dialog ending",
                    )
                    stop_reason = DialogStopReason.STOP_TOKEN
                    break

                current_prompt = sim_result.text
                self._log_conversation("USER", turns_completed + 1, current_prompt)

            # Final criteria check for end_of_dialog (or both) modes, or when
            # every_turn mode did not run a check for the final turn.
            end_of_dialog_needed = (
                sim_config.check_criteria in ("end_of_dialog", "both") or not criteria_checked_this_turn
            )
            if end_of_dialog_needed:
                reference_code, reference_dir, self._reference_code = load_reference(
                    task=self.task,
                    task_file=self.task_file,
                    cached_reference=self._reference_code,
                )
                criteria_results = await asyncio.to_thread(
                    self.success_checker.check_all,
                    self.task.success_criteria,
                    reference_code=reference_code,
                    reference_dir=reference_dir,
                    turn_records=self.result.iterations,
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
            # If an agent turn was attempted but crashed before attaching pending_user_turn to a turn_record,
            # append it as a standalone entry so its telemetry is not lost. (If the simulator's opener
            # had stop_requested, we never entered the agent loop, so don't record it.)
            if agent_turn_attempted and pending_user_turn is not None and self.result is not None:
                standalone_turn = TurnRecord(
                    iteration=turns_completed,
                    user_input=pending_user_turn.text or "",
                    agent_output="",
                    messages=[pending_user_turn],
                )
                self.result.iterations.append(standalone_turn)

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

    def _log_conversation(self, role: str, turn: int, text: str, metadata: str = "") -> None:
        """Append one utterance to conversation.log.

        Role is "USER" (from the simulator, or the pinned initial_prompt) or
        "AGENT". Called from the simulation dialog loop only — single-shot
        runs do not produce a conversation.log. ``run_dir`` is guaranteed to
        exist by the time this runs (``task_log_handler`` creates it before
        the dialog loop starts).
        """
        header = f"=== {role} (turn {turn}){f' — {metadata}' if metadata else ''} ==="
        body = _extract_utterance(text or "").rstrip()
        with self.conversation_log_path.open("a", encoding="utf-8") as f:
            f.write(f"{header}\n{body}\n\n")

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

    _POST_RUN_MAX_OUTPUT = 100_000  # Truncate stdout/stderr to 100KB
    _POST_RUN_STREAM_LIMIT = 262_144  # StreamReader per-line buffer (256KB)

    async def _run_command_list(
        self,
        commands: list[PreRunCommand] | list[PostRunCommand],
        results: list[PostRunResult],
        label: str,
    ) -> None:
        """Run a list of shell commands inside the sandbox, capturing output.

        stdout/stderr are streamed line-by-line to the orchestrator logger and
        accumulated into ``results`` for the report (truncated to
        ``_POST_RUN_MAX_OUTPUT`` per stream). ``label`` is used in stream/log
        labels (e.g. ``"pre_run"`` -> ``[pre_run stdout]``).

        For commands carrying ``fail_on_error=True`` (PreRunCommand only), a
        non-zero exit, timeout, or exception appends the failure result and then
        raises ``RuntimeError``, aborting the loop. PostRunCommand never has
        ``fail_on_error`` set, so failures are warning-logged and the loop
        continues — preserving existing post-run "informational only" semantics.
        """
        if not commands or not self.sandbox or not self.sandbox.sandbox_dir or not self.result:
            return

        sandbox_dir = self.sandbox.sandbox_dir
        max_out = self._POST_RUN_MAX_OUTPUT
        human = label.replace("_", "-").capitalize()  # "pre_run" -> "Pre-run"

        for cmd in commands:
            fail_on_error = isinstance(cmd, PreRunCommand) and cmd.fail_on_error
            start = time.time()
            logger.info("Running %s command: %s", human.lower(), cmd.command)

            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd.command,
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
                            _pump_stream(proc.stdout, logger.info, f"{label} stdout", stdout_chunks),
                            _pump_stream(proc.stderr, logger.warning, f"{label} stderr", stderr_chunks),
                            proc.wait(),
                        ),
                        timeout=cmd.timeout,
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    results.append(
                        PostRunResult(
                            command=cmd.command,
                            stdout="".join(stdout_chunks)[:max_out],
                            stderr="".join(stderr_chunks)[:max_out],
                            error=f"Timed out after {cmd.timeout}s",
                            duration_seconds=time.time() - start,
                        )
                    )
                    if fail_on_error:
                        raise RuntimeError(f"{human} command timed out after {cmd.timeout}s: {cmd.command!r}") from None
                    logger.warning("%s command '%s' timed out after %ds", human, cmd.command, cmd.timeout)
                    continue

                stdout_text = "".join(stdout_chunks)[:max_out]
                stderr_text = "".join(stderr_chunks)[:max_out]
                results.append(
                    PostRunResult(
                        command=cmd.command,
                        exit_code=proc.returncode,
                        stdout=stdout_text,
                        stderr=stderr_text,
                        duration_seconds=time.time() - start,
                    )
                )
                if proc.returncode != 0:
                    if fail_on_error:
                        raise RuntimeError(f"{human} command failed (exit {proc.returncode}): {cmd.command!r}")
                    logger.warning(
                        "%s command '%s' exited with code %d: %s",
                        human,
                        cmd.command,
                        proc.returncode,
                        stderr_text[:200],
                    )
            except RuntimeError:
                # Propagate abort signal from fail_on_error=True branches unchanged;
                # otherwise the catch-all below would re-wrap it as a new RuntimeError.
                raise
            except Exception as e:
                results.append(
                    PostRunResult(
                        command=cmd.command,
                        error=str(e),
                        duration_seconds=time.time() - start,
                    )
                )
                if fail_on_error:
                    raise RuntimeError(f"{human} command failed: {cmd.command!r}") from e
                logger.warning("%s command '%s' failed: %s", human, cmd.command, e)

    async def _run_pre_run_commands(self) -> None:
        """Execute pre-run commands inside the sandbox before evaluation.

        See ``_run_command_list``. A failing command with ``fail_on_error=True``
        (the default) raises ``RuntimeError``, which propagates to ``run()``'s
        outer ``except Exception`` handler and lands the run as
        ``FinalStatus.ERROR``. Post-run commands and cleanup still execute via
        the ``finally`` block.
        """
        if self.result is None:
            return
        await self._run_command_list(self.task.pre_run, self.result.pre_run_results, "pre_run")

    async def _run_post_run_commands(self) -> None:
        """Execute post-run commands inside the sandbox after evaluation.

        See ``_run_command_list``. Post-run commands are informational only —
        ``fail_on_error`` is not part of ``PostRunCommand``, so failures are
        warning-logged and never affect the evaluation verdict.
        """
        if self.result is None:
            return
        await self._run_command_list(self.task.post_run, self.result.post_run_results, "post_run")

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
