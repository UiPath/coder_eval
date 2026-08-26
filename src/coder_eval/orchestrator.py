"""Main orchestrator for coordinating task evaluation."""

import asyncio
import logging
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from inspect import isawaitable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from .agent import Agent
from .agents.watchdog import ThreadedWatchdog
from .analysis import calculate_command_statistics
from .config import settings
from .criteria.commands_efficiency import compute_commands_efficiency
from .errors import (
    AgentCrashError,
    BudgetExceededError,
    ReferenceTamperedError,
    TaskTimeoutError,
    TurnTimeoutError,
)
from .errors.executor import execute_with_retry
from .errors.retry import create_error_context
from .evaluation.checker import SuccessChecker, _short_failure_reason
from .litellm_cost import apply_actual_cost, load_cost_records
from .models import (
    CONTAINER_REFERENCE_DIR,
    DEFAULT_STOP_EARLY_GATE_THRESHOLD,
    ROUTE_NAMES,
    AgentKind,
    ApiRoute,
    BedrockRoute,
    ConfigLineageEntry,
    CriteriaResults,
    CriterionResult,
    DirectRoute,
    EvaluationResult,
    FinalStatus,
    JudgeCriterionResult,
    LiteLLMRoute,
    PostRunCommand,
    PostRunResult,
    PreRunCommand,
    PreservationMode,
    ReferenceComparisonCriterion,
    SimulationConfig,
    SimulationTelemetry,
    SuccessCriterion,
    TaskConfigRecord,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
    UserMessage,
    resolve_evaluation_route,
    resolve_route,
)
from .orchestration.early_stop import EarlyStopWatcher, early_stop_active, validate_early_stop
from .orchestration.evaluation import resolve_reference_dir, stage_reference_dir
from .orchestration.run_limits import validate_run_limits
from .path_utils import digest_tree, format_task_log_id, rmtree_restrictive, task_log_path
from .sandbox import Sandbox
from .simulation import DialogStopReason, SimulatorResult, UserSimulator, evaluate_stop
from .streaming.callbacks import CompositeStreamCallback, StreamCallback, TaskScopedCallback, safe_emit
from .streaming.events import CriteriaCheckEvent, CriterionSummary
from .telemetry import Scalar, hash_identifier
from .utils import get_version_info, looks_like_version, runtime_uip_versions


# Get module logger
logger = logging.getLogger(__name__)


# Grace on outer wait_for so the agent's in-band watchdog (which preserves a partial)
# wins the race against the asyncio cancel path (which doesn't).
_WAIT_FOR_GRACE_SECONDS = 2.0


def _close_subprocess_transport(proc: asyncio.subprocess.Process | None) -> None:
    """Release a finished subprocess's pipe transport deterministically.

    ``asyncio`` gives ``Process`` no public way to do this: the transport is held
    privately and is only closed when the object is garbage-collected. On
    Windows' Proactor loop ``_ProactorBasePipeTransport.__del__`` then emits
    ``ResourceWarning: unclosed transport`` — one per pipe — which a strict test
    run surfaces as ``PytestUnraisableExceptionWarning`` attributed to whichever
    test happened to be running when the GC fired, not to the code that leaked.
    That is exactly the shape of the intermittent Windows CI failures in
    ``test_pre_run`` / ``test_post_run`` / ``test_preservation_mode``.

    Best-effort and idempotent: a second ``close()`` is a no-op, and any error is
    swallowed because releasing a pipe must never turn into a task failure.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    with suppress(Exception):
        transport.close()


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


class EvalRouteOverrides(NamedTuple):
    """``checker_context.api_route``'s ``(backend, model)`` pair. Named fields
    (rather than a bare tuple) so a future transposition at a call site is a
    typo'd attribute, not a silent positional swap of backend vs. model."""

    backend: str | None
    model: str | None


def _format_routing(route: ApiRoute, effective_model: str | None = None) -> str:
    """Format the route name for the ``API routing:`` log line.

    For ``DirectRoute`` the resolved judge transport is appended so the choice
    (anthropic / none) is visible on every run, not only in the persisted
    ``environment_info`` record. For ``LiteLLMRoute`` the model is shown — the
    ``effective_model`` (the resolved ``agent.model``, e.g. from ``--model``)
    when supplied, else the route's own default — so the line reflects what the
    agent will actually send rather than the route-level fallback.
    """
    name = ROUTE_NAMES[type(route)]
    if isinstance(route, DirectRoute):
        return f"{name} (judge transport: {route.judge_transport or 'none'})"
    if isinstance(route, LiteLLMRoute):
        return f"{name} (model: {effective_model or route.model or 'default'})"
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


def build_task_event(result: EvaluationResult, *, driver: str, variant_id: str) -> tuple[str, dict[str, Scalar]]:
    """Build the (event_name, properties) for a finalized task's telemetry event.

    Shared by the in-process path (``Orchestrator._finalize_result``) and the
    docker path (``orchestration/batch.py``) so both drivers emit an identical
    ``CoderEval.Task.End`` event. Carries only enums/counts/durations/config-derived
    ids — no user content. None-safe.

    Every task emits the SAME event name (``CoderEval.Task.End``); the outcome
    lives in dimensions, never the name. ``Status`` carries the exact
    ``FinalStatus`` and ``Category`` carries the canonical ``FinalStatus.category``
    bucket (``succeeded`` / ``failed`` / ``error``) — the single source of truth
    shared with reports. Slicing belongs in dimensions (the App Insights idiom),
    so dashboards group by ``Status``/``Category`` rather than matching event
    names, and the telemetry bucketing can never drift from ``category``.

    The return type is the scalar event contract (``dict[str, Scalar]``) so a
    non-scalar property is caught here by pyright, not just str()-coerced at
    runtime by the telemetry layer. Token counts are intentionally NOT emitted —
    this is usage telemetry, not eval analytics. Task/variant ids are emitted as
    stable one-way hashes (``hash_identifier``) so an author-defined free-text id
    that could encode sensitive data never reaches the telemetry store verbatim.
    """
    props: dict[str, Scalar] = {
        "TaskId": hash_identifier(result.task_id),
        "VariantId": hash_identifier(variant_id),
        "Status": result.final_status.value,
        "Category": result.final_status.category,
        "DurationMs": int((result.duration_seconds or 0.0) * 1000),
        "Score": float(result.weighted_score or 0.0),
        "Iterations": result.iteration_count,
        "AgentType": result.agent_type or "",
        "Model": result.model_used or "",
        "Driver": driver,
        "EarlyStopped": result.early_stop is not None,
        "EarlyStopReason": (result.early_stop.reason.value if result.early_stop is not None else ""),
    }
    return "CoderEval.Task.End", props


@dataclass
class _SolicitedMessage:
    """Result of asking the user simulator for one utterance (opener or in-loop).

    ``message is None`` signals a simulator failure (the caller bumps
    ``simulator_failures`` and reacts); otherwise the caller inspects
    ``message.stop_requested`` and folds ``sim_in``/``sim_out`` into its running
    token totals. Module-private — NOT a public model.
    """

    message: UserMessage | None
    sim_in: int
    sim_out: int


@dataclass
class _OpenerOutcome:
    """Result of the pure-simulation opener acquisition (``initial_prompt is None``).

    When ``short_circuit`` is True the opener already wrote simulation telemetry and the
    dialog loop must ``return return_value`` immediately (simulator failure → ERROR, or an
    opener carrying the stop token → STOP_TOKEN, both before any agent turn). Otherwise the
    caller folds ``sim_in``/``sim_out``/``failures`` into its counters and binds
    ``current_prompt``/``pending_user_turn``. Module-private — NOT a public model.
    """

    short_circuit: bool
    return_value: bool = False
    current_prompt: str | None = None
    pending_user_turn: UserMessage | None = None
    sim_in: int = 0
    sim_out: int = 0
    failures: int = 0


class Orchestrator:
    """Coordinates the full evaluation loop for a task.

    Manages the sandbox, agent, and evaluators to run a complete
    task evaluation with multiple iterations.
    """

    def __init__(
        self,
        task: TaskDefinition,
        run_dir: Path,
        preservation_mode: PreservationMode = PreservationMode.MOVE_ON_WRITE,
        task_file: Path | None = None,
        stream_callback: StreamCallback | None = None,
        sandbox: Sandbox | None = None,
        *,
        variant_id: str,
        source_yaml: str = "",
        config_lineage: dict[str, ConfigLineageEntry] | None = None,
        replicate_index: int = 0,
        workspace_dir: Path | None = None,
    ):
        """Initialize the orchestrator.

        Args:
            task: Task definition to evaluate
            run_dir: Per-task directory within a run (e.g., runs/2025-10-09_15-30-45/default/hello_date/00/)
            preservation_mode: How to persist the sandbox after completion
                (NONE / MOVE_ON_WRITE / DIRECT_WRITE). The driver-derived
                default is resolved upstream at the batch dispatch seam.
            task_file: Path to task YAML file (for resolving reference file paths)
            stream_callback: Optional callback for real-time event streaming
            sandbox: Pre-built Sandbox to use directly; if None, creates one from task config and runs the agent
            variant_id: Experiment variant identifier for this task
            source_yaml: Raw YAML text from the task file
            config_lineage: Config lineage dict (dotted-path -> ConfigLineageEntry)
            replicate_index: Zero-indexed trial number (for simulation tasks with n_trials > 1).
                Defaults to 0, which covers single-shot tasks and single-trial simulations.
            workspace_dir: Docker WORKDIR alignment. When set, the agent runs
                in-place at this absolute container path (the task image's WORKDIR) instead of
                run_dir/artifacts/<task>, and the workspace is copied out to run_dir/artifacts/<task>
                at cleanup. Resolved host-side by DockerRunner; None keeps standard behavior.
                Takes precedence over preservation_mode when set.
        """
        self.task = task
        self.run_dir = run_dir
        # Per-attempt nonce for the LiteLLM cost-log join. The proxy log is
        # append-only and the run_id is a deterministic hash of run_dir, so a
        # re-run into the same --run-dir would otherwise re-match (and double-count)
        # a prior attempt's rows. A fresh nonce per Orchestrator (one per process
        # invocation) scopes the join to THIS attempt's records.
        self._cost_attempt_nonce = uuid.uuid4().hex
        self.preservation_mode = preservation_mode
        self.workspace_dir = workspace_dir
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
        self.agent: Agent[Any] | None = None
        self.success_checker: SuccessChecker | None = None

        # API routing (initialized in _setup)
        self.route: ApiRoute | None = None
        # Route for the evaluation side (llm_judge / agent_judge / simulated user):
        # pinned to a constant Claude backend so grading stays comparable when the
        # agent runs on an open-weight (LiteLLM) model. Equals self.route for the
        # Direct/Bedrock backends.
        self.eval_route: ApiRoute | None = None

        # Result tracking
        self.result: EvaluationResult | None = None

        # Per-run private copy of task.reference.directory, staged in _setup and
        # removed in _cleanup. Criteria address it as $REFERENCE_DIR / REFERENCE_DIR.
        # It is a COPY, not the checked-out path, so the mode-000 anti-cheat window
        # around each agent turn can't block a sibling task's judge mid-read, and a
        # crashed run can only leave a throwaway directory unreadable.
        self._reference_dir: Path | None = None

        # The mkdtemp root that holds ``_reference_dir``, recorded the moment it
        # is created and BEFORE the copy runs, so a copy that raises part-way
        # (unreadable source file, ENOSPC) still gets cleaned up. Keying cleanup
        # on ``_reference_dir.parent`` instead would leak the partial copy of the
        # reference solution, because ``_reference_dir`` is only assigned on the
        # success path. None under docker, where the reference is a host-owned
        # bind mount rather than a tempdir of ours.
        self._reference_staging_root: Path | None = None

        # SHA-256 of the staged reference tree, taken right after staging and
        # re-verified before grading. The window is per-turn, so between turns
        # the (necessarily writable) docker mount is back at its normal mode; an
        # agent-backgrounded process could overwrite the reference and drive
        # ``reference_comparison`` to 1.0. See _verify_reference_integrity.
        self._reference_digest: str | None = None

        # Early-stop watcher (created in _setup only when a criterion carries a
        # stop_early: block and the kill switch is not thrown; None otherwise,
        # so the default path is entirely unaffected).
        self._early_stop_watcher: EarlyStopWatcher | None = None

        # One-shot flag: emit the "cost budget configured but no cost data" warning
        # exactly once per task even if _check_run_limits fires every turn.
        self._cost_budget_skipped_logged: bool = False

        # One-shot flag: emit the expected_turns rollup warning exactly once per
        # task run even though _check_expected_turns is called after every turn.
        self._expected_turns_warning_emitted: bool = False

        # One-shot flag: a resolved task may be inspected more than once during
        # setup, but its ineffective timeout relationship should be logged once.
        self._run_limits_warning_emitted: bool = False

        # Canonical id shared with run_dir layout, tqdm label, and streaming events.
        self._log_task_id = format_task_log_id(variant_id, task.task_id, replicate_index)

    @property
    def _agent_name(self) -> str:
        """Agent name for error-context telemetry.

        The agent kind string for any resolved task (``"none"`` for no-op tasks);
        falls back to ``"none"`` only on the evaluate-only path, where no agent
        is attached. ``str(...)`` handles both built-in ``AgentKind`` members and
        plugin-registered raw-string kinds.
        """
        if self.task.agent is not None and self.task.agent.type is not None:
            return str(self.task.agent.type)
        return AgentKind.NONE.value

    async def run(self) -> EvaluationResult:
        """Run the complete evaluation.

        Returns:
            Evaluation result with all details

        Raises:
            RuntimeError: If evaluation fails catastrophically
        """
        from .logging_config import task_log_handler

        # Agent must be resolved before reaching the orchestrator. No-op (type: none)
        # tasks are resolved to a NoneAgentConfig like any other agent, so there is
        # no separate "no agent" branch here.
        assert self.task.agent is not None, (
            f"Task '{self.task.task_id}' has no agent config. Ensure experiment resolution ran before orchestration."
        )
        assert self.task.agent.type is not None, (
            f"Task '{self.task.task_id}' has no agent.type. Ensure experiment resolution + CLI overrides "
            "ran before orchestration."
        )
        agent_type = self.task.agent.type

        start_time = time.time()
        started_at = datetime.now()

        # Initialize result
        self.result = EvaluationResult(
            task_id=self.task.task_id,
            task_description=self.task.description,
            variant_id=self.variant_id,
            agent_type=agent_type,
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
                    success = await self._run_evaluation_with_failure_evidence(
                        watchdog=wd,
                        task_timeout=task_timeout,
                        start_time=start_time,
                    )
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
                    agent_name=self._agent_name,
                )

                logger.error(f"Task timed out: {e}")

                # Recover the turn in flight when the watchdog killed the agent.
                # Nothing else on this path does: the cancel arrives as a
                # BaseException, so it never reaches the retry executor's
                # per-attempt hook that drains the slot on a turn-level timeout.
                await self._drain_killed_turn()
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
                    agent_name=self._agent_name,
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
                    agent_name=self._agent_name,
                )

                logger.error(f"Evaluation failed: {e}", exc_info=True)

            finally:
                # Teardown must be interrupt-proof: the task-timeout watchdog can
                # fire while post-run commands are awaiting and deliver its
                # CancelledError right here in the finally block, which used to
                # abort it wholesale — skipping _cleanup() (tempdir leaked) AND
                # _finalize_result() (task.json lost, so the task silently drops
                # out of the run). Catch the interrupt, finish the full teardown,
                # then re-raise it at the end so callers observe the same exception
                # as before. The watchdog cancels exactly once, so the teardown
                # awaits below run normally after the CancelledError is caught.
                teardown_interrupt: BaseException | None = None
                try:
                    # BEFORE post-run/cleanup: needs the live sandbox to resolve
                    # the agent-aligned `uip`, and post-task tool state on disk.
                    self._refresh_runtime_tool_versions()
                    await self._run_post_run_commands()
                except (Exception, asyncio.CancelledError) as e:
                    teardown_interrupt = e
                    logger.warning(
                        "Teardown interrupted during post-run (%s: %s); completing cleanup before re-raising",
                        type(e).__name__,
                        e,
                    )
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
                if teardown_interrupt is not None:
                    raise teardown_interrupt

        return self.result

    async def _run_evaluation_with_failure_evidence(
        self,
        *,
        watchdog: ThreadedWatchdog,
        task_timeout: int | None,
        start_time: float,
    ) -> bool:
        """Run the loop and collect diagnostics before its watchdog closes."""
        try:
            return await self._evaluation_loop()
        except asyncio.CancelledError:
            if watchdog.fired:
                self._record_post_failure_not_evaluated(
                    "the task_timeout budget was exhausted before post-failure grading could run"
                )
                raise TaskTimeoutError(
                    task_timeout or 0,
                    task_id=self.task.task_id,
                    elapsed_seconds=time.time() - start_time,
                ) from None
            raise
        except (AgentCrashError, TurnTimeoutError, BudgetExceededError) as terminal_error:
            if (
                isinstance(terminal_error, BudgetExceededError)
                and self.result is not None
                and len(self.result.success_criteria_results) == len(self.task.success_criteria)
            ):
                raise
            try:
                await self._evaluate_post_failure_criteria()
            except asyncio.CancelledError:
                if watchdog.fired:
                    self._record_post_failure_not_evaluated(
                        "the task_timeout budget expired during post-failure grading"
                    )
                    logger.warning(
                        "[%s] Task timeout stopped post-failure grading; preserving %s",
                        self.task.task_id,
                        type(terminal_error).__name__,
                    )
                else:
                    raise
            except Exception as recovery_error:
                self._record_post_failure_not_evaluated(self._post_failure_exception_reason(recovery_error))
                logger.warning(
                    "[%s] Post-failure criteria evaluation failed; preserving the original terminal error",
                    self.task.task_id,
                    exc_info=True,
                )
            raise

    @staticmethod
    def _post_failure_exception_reason(error: Exception) -> str:
        message = " ".join(str(error).split())
        if len(message) > 200:
            message = message[:199] + "…"
        suffix = f": {message}" if message else ""
        return f"post-failure grading could not complete ({type(error).__name__}{suffix})"

    @staticmethod
    def _not_evaluated_result(criterion: SuccessCriterion, reason: str) -> CriterionResult:
        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=0.0,
            details=f"Not evaluated after terminal agent failure: {reason}.",
            evaluation_status="not_evaluated",
            pass_threshold=criterion.pass_threshold,
            gating=criterion.is_gating,
        )

    def _record_post_failure_not_evaluated(self, reason: str) -> None:
        """Record a full diagnostic result vector when recovery cannot run."""
        if self.result is None:
            return
        self.result.post_failure_criteria_results = [
            self._not_evaluated_result(criterion, reason) for criterion in self.task.success_criteria
        ]

    async def _evaluate_post_failure_criteria(self) -> None:
        """Evaluate diagnostic criteria before the live sandbox is torn down.

        Results stay outside the canonical scored list. Only criteria that
        declare themselves deterministic and read-only run on this path. This
        excludes judges and checks that execute sandbox commands.
        """
        if self.result is None:
            return
        if self.success_checker is None or self.sandbox is None:
            self._record_post_failure_not_evaluated("the sandbox or success checker was unavailable")
            return

        runnable: list[SuccessCriterion] = []
        unavailable_positions: set[int] = set()
        for position, criterion in enumerate(self.task.success_criteria):
            if not criterion.supports_post_failure_evaluation:
                unavailable_positions.add(position)
            else:
                runnable.append(criterion)

        checked: CriteriaResults = []
        if runnable:
            checked = await self.success_checker.check_all_async(
                runnable,
                reference_dir=self._reference_dir,
                turn_records=self.result.iterations,
            )

        if len(checked) != len(runnable):
            raise ValueError(
                f"Post-failure checker returned {len(checked)} results for {len(runnable)} runnable criteria"
            )

        checked_iter = iter(checked)
        recovered: CriteriaResults = []
        for position, criterion in enumerate(self.task.success_criteria):
            if position in unavailable_positions:
                recovered.append(
                    self._not_evaluated_result(
                        criterion,
                        "the criterion is not a deterministic, read-only artifact check",
                    )
                )
            else:
                recovered.append(next(checked_iter))
        self.result.post_failure_criteria_results = recovered

    async def _drain_killed_turn(self) -> None:
        """Move a hard-killed turn's partial record from the agent onto the result.

        The only reader of ``pending_turn`` on the task-timeout path. Ordering
        matters both ways: it must run before ``_cleanup`` (whose ``agent.stop()``
        clears the slot) and before ``_finalize_result``, so the recovered turn
        feeds token aggregation and command stats like any other.

        Best-effort: a task killed before its first turn has nothing parked, and
        this runs on the way to a saved row, so it must not raise.
        """
        if self.agent is None or self.result is None:
            return
        try:
            partial = self.agent.pending_turn
            # `pending_turn` is a slot any agent implementation fills, so a non-record
            # here would fail validation during teardown and take the row down with it.
            if not isinstance(partial, TurnRecord):
                logger.debug("[%s] Hard-killed task preserved no partial turn", self.task.task_id)
                return
            self.result.iterations.append(partial)
            await self.agent.discard_pending_turn()
            usage = partial.token_usage
            logger.info(
                "[%s] Recovered the hard-killed turn: %d tokens, %s",
                self.task.task_id,
                usage.total_tokens if usage is not None else 0,
                f"${usage.total_cost_usd:.4f}"
                if usage is not None and usage.total_cost_usd is not None
                else "unpriced",
            )
        except Exception:
            logger.warning("[%s] Could not recover the hard-killed turn", self.task.task_id, exc_info=True)

    def _finalize_result(self, start_time: float) -> None:
        """Finalize the evaluation result: scores, telemetry, and persistence."""
        if not self.result:
            return

        # Every resolved task carries an agent config (no-op tasks resolve to a
        # NoneAgentConfig); a missing one is a resolution bug. The evaluate-only
        # path doesn't reach here with task.agent unset.
        if self.task.agent is None:
            logger.error("Cannot finalize result: task.agent is None")
            return

        self.result.completed_at = datetime.now()
        self.result.duration_seconds = time.time() - start_time

        # Weighted score. This call site is wrapped because _finalize_result runs
        # inside run()'s finally — an unguarded raise here would skip persistence and
        # lose task.json. The other calculate_weighted_score calls (the simulation
        # path) run inside run()'s try, whose broad `except Exception` already converts
        # a raise into a populated ERROR result, so they intentionally stay unwrapped.
        try:
            self.result.calculate_weighted_score(self.task.success_criteria)
        except ValueError as e:
            logger.error("Weighted-score computation failed; marking row ERROR: %s", e, exc_info=True)
            self.result.weighted_score = None
            self.result.final_status = FinalStatus.ERROR
            self.result.error_message = str(e)
            self.result.error_details = create_error_context(
                error=e,
                task_id=self.task.task_id,
                attempt=max(self.result.iteration_count, 1),
                component="orchestrator.finalize.weighted_score",
                agent_name=self._agent_name,
            )

        # Command statistics
        if self.result.iterations:
            self.result.command_stats = calculate_command_statistics(self.result.iterations)

        # Resolve model_used (last turn with model wins, then agent config)
        if self.result.iterations:
            for turn in reversed(self.result.iterations):
                if turn.model_used:
                    self.result.model_used = turn.model_used
                    break
        if not self.result.model_used and self.task.agent is not None and self.task.agent.model:
            self.result.model_used = self.task.agent.model

        # Open-weight (LiteLLM) backend: replace per-turn cost with the ACTUAL
        # per-call OpenRouter cost captured proxy-side. Runs BEFORE aggregation so
        # the run total re-derives from the corrected per-turn costs.
        self._join_litellm_actual_cost()

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

        # Usage telemetry (non-fatal; placed before persistence since track_event
        # cannot raise). For the docker driver this in-process emit runs INSIDE the
        # container where telemetry is off (the connection-string env vars aren't
        # forwarded), so the host emits the event from batch.py instead — see
        # build_task_event. Non-docker tasks finalize on the host and emit here.
        from .telemetry import track_event

        driver = self.task.sandbox.driver if self.task.sandbox else ""
        name, props = build_task_event(self.result, driver=driver, variant_id=self.variant_id or "")
        track_event(name, props)

        # Persist
        self.report_path.parent.mkdir(parents=True, exist_ok=True)  # noqa: CE002 — mkdir on local FS is nanoseconds

        # Spill any judge transcripts to sibling YAML files BEFORE
        # we dump task.json, so transcript_path is set on each judge result.
        # The inline `transcript` field stays in memory — HTML rendering below
        # uses it directly. We strip it from the JSON dump via `exclude=...`.
        from .evaluation.judge_persistence import TASK_JSON_TRANSCRIPT_EXCLUDE, spill_judge_transcripts

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
                # Strip inline transcripts: they live in sibling YAML files
                # next to task.json, referenced by transcript_path. Excluding
                # `transcript` here avoids ~20-100 KB of bloat per judge result
                # in the row record without losing any data.
                exclude=TASK_JSON_TRANSCRIPT_EXCLUDE,
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

        input_tokens = sum(u.uncached_input_tokens for u in usages)
        if limits.count_cache_creation:
            input_tokens += sum(u.cache_creation_input_tokens for u in usages)
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
                + "for task %s. Run continues — max_turns remains the hard cap.",
                total,
                limits.expected_turns,
                iteration,
                self.task.task_id,
            )
            self._expected_turns_warning_emitted = True

    def _warn_on_ineffective_task_timeout(self) -> None:
        """Log resolved cross-field run-limit warnings once per task run."""
        if self._run_limits_warning_emitted:
            return
        messages = validate_run_limits(self.task)
        for message in messages:
            logger.warning("[%s] %s", self.task.task_id, message)
        if messages:
            self._run_limits_warning_emitted = True

    @property
    def _cost_correlation_run_id(self) -> str:
        """The LiteLLM cost-log correlation run id — a stable hash of the run dir.

        Single accessor used by BOTH the stamp site (``_create_agent``, into
        ``x-ce-run-id``) and the join site (``_join_litellm_actual_cost``); keeping
        the derivation in one place means the two can't drift and silently revert
        every turn to static pricing.
        """
        return hash_identifier(self.run_dir.as_posix())

    def _join_litellm_actual_cost(self) -> None:
        """Override per-turn cost with the proxy-captured ACTUAL per-call OpenRouter
        cost (and attach the per-call cache breakdown) for the open-weight backend.

        No-op unless the agent ran on a ``LiteLLMRoute`` AND ``LITELLM_COST_LOG`` is
        configured. Never fatal: a failure, or an empty/mismatched log, leaves each
        turn's static rate-card estimate in place (the whole-turn fallback).
        """
        if not (isinstance(self.route, LiteLLMRoute) and settings.litellm_cost_log and self.result is not None):
            return
        try:
            applied = apply_actual_cost(
                self.result,
                run_id=self._cost_correlation_run_id,
                task_id=self._log_task_id,
                attempt=self._cost_attempt_nonce,
                records=load_cost_records(settings.litellm_cost_log),
            )
            if applied:
                logger.info("LiteLLM actual-cost join: real per-call cost applied to %d turn(s)", applied)
            else:
                # Tags were stamped but nothing matched (file absent, proxy never
                # wrote, wrong path, or a run/task/attempt mismatch). The run stays
                # on the static rate card — warn so it isn't mistaken for the real bill.
                logger.warning(
                    "LiteLLM actual-cost join found no matching records in %s (run=%s task=%s); cost stays static",
                    settings.litellm_cost_log,
                    self._cost_correlation_run_id,
                    self._log_task_id,
                )
        except Exception:
            logger.warning("LiteLLM actual-cost join failed; keeping static pricing", exc_info=True)

    def _aggregate_token_usage(self) -> None:
        """Aggregate token usage from turns, storing on self.result.

        Per-turn ``TurnRecord.token_usage`` is filled by the agent SDK's
        ResultMessage on DirectRoute / BedrockRoute (the bundled CLI parses
        the response's ``usage`` field). Every iteration carries the right
        per-turn value here, and we just sum them. Judge / sub-agent token
        usage is captured on
        the corresponding ``JudgeCriterionResult.token_usage`` and is
        intentionally NOT included in this aggregate — it represents the
        main agent's bill, not the eval-machinery overhead.
        """
        assert self.result is not None

        # Include crashed=True partials: each API call is billed independently.
        if self.result.iterations:
            usages = [t.token_usage for t in self.result.iterations if t.token_usage is not None]
            if usages:
                costs = [u.total_cost_usd for u in usages if u.total_cost_usd is not None]
                self.result.total_token_usage = TokenUsage(
                    uncached_input_tokens=sum(u.uncached_input_tokens for u in usages),
                    output_tokens=sum(u.output_tokens for u in usages),
                    cache_creation_input_tokens=sum(u.cache_creation_input_tokens for u in usages),
                    cache_read_input_tokens=sum(u.cache_read_input_tokens for u in usages),
                    total_cost_usd=sum(costs) if costs else None,
                )

    async def _stage_reference(self) -> None:
        """Resolve the reference directory this run will expose as REFERENCE_DIR.

        No-op when the task declares no reference. What the anti-cheat window
        chmods is whatever this sets, so it MUST be the same path the agent can
        reach — otherwise the window shields a decoy while the agent reads the
        real thing (exactly the leak ``tasks/anti_cheat_reference`` caught).

        Under ``driver: docker`` that path is the ``/work/references`` mount
        itself: the host already bind-mounts a throwaway COPY there read-write
        (``DockerRunner._prepare_reference_mount``) precisely so this process can
        chmod it. Re-copying it here would be worse than pointless — it would
        move the shielded path off the one the agent attacks.

        On the host there is no container boundary and the window is a no-op
        (see ``Sandbox.enforces_permission_windows``), but we still stage a
        private copy: it strips symlinks and keeps ``$REFERENCE_DIR`` semantics
        identical across drivers.
        """
        source = resolve_reference_dir(self.task, self.task_file)
        if source is None:
            return
        if source == Path(CONTAINER_REFERENCE_DIR):
            self._reference_dir = source
        else:
            # Record the root BEFORE the copy: _cleanup keys on this field, so a
            # copytree that raises part-way must still leave something to remove.
            staging = Path(tempfile.mkdtemp(prefix="coder_eval_reference_"))
            self._reference_staging_root = staging
            # copytree needs a non-existent leaf; mkdtemp already made the parent.
            destination = staging / "reference"
            self._reference_dir = await asyncio.to_thread(stage_reference_dir, source, destination)
        self._reference_digest = await asyncio.to_thread(digest_tree, self._reference_dir)
        self._validate_reference_consumers()

    def _validate_reference_consumers(self) -> None:
        """Fail fast on a criterion that names a reference file which isn't there.

        A typo in ``reference_comparison.reference_file`` is an EVAL-CONFIG error,
        not an agent failure. Left to check time it surfaces as a gating
        ``score=0.0`` → ``FinalStatus.FAILURE``, i.e. counted against the agent's
        pass rate — and on a dataset-fanned suite it zeroes every row silently.
        Raising here routes it to ``FinalStatus.ERROR`` instead, and does so
        before the agent burns a single token.
        """
        reference_dir = self._reference_dir
        if reference_dir is None:
            return
        for criterion in self.task.success_criteria:
            if not isinstance(criterion, ReferenceComparisonCriterion):
                continue
            candidate = (reference_dir / criterion.reference_file).resolve()
            if not candidate.is_relative_to(reference_dir.resolve()) or not candidate.is_file():
                raise ValueError(
                    f"reference_comparison.reference_file {criterion.reference_file!r} does not name a "
                    + f"readable file inside the task's reference directory ({self.task.reference.directory!r} "  # type: ignore[union-attr]
                    + "— task.reference is guaranteed set here by the load-time validator). "
                    + "This is a task-definition error, not an agent failure."
                )

    async def _verify_reference_integrity(self) -> None:
        """Re-hash the reference before grading and hard-fail on a mismatch.

        The mode-000 window spans ``agent.communicate`` only, and nothing reaps
        the agent's child processes at turn end — so a backgrounded writer can
        modify the reference once the window closes. Under docker the mount MUST
        be writable (chmod on a ``:ro`` bind fails with EROFS), so this is the
        control that stops an agent from overwriting the solution with its own
        file and scoring ``reference_comparison`` 1.0.

        Read-back is not covered by this; see the KNOWN GAP in ``fs_permissions``.
        """
        if self._reference_dir is None or self._reference_digest is None:
            return
        current = await asyncio.to_thread(digest_tree, self._reference_dir)
        if current != self._reference_digest:
            raise ReferenceTamperedError(
                "The reference solution changed during the run: its content hash before the agent ran "
                + f"({self._reference_digest[:12]}...) does not match the hash at grading time "
                + f"({current[:12]}...). Refusing to grade against a reference the agent may have written."
            )

    async def _setup(self) -> None:
        """Set up all components for evaluation.

        Raises:
            RuntimeError: If setup fails
        """
        # Defensive early-stop guardrails for the library-use and in-container
        # paths (the CLI already validated during resolution). No-op unless
        # some criterion carries a stop_early: block.
        validate_early_stop(self.task)
        self._warn_on_ineffective_task_timeout()

        # Build the early-stop watcher once, up front, when armed (>= 1 criterion
        # with a stop_early: block and the run_limits.stop_early kill switch not
        # thrown). This sits BEFORE the evaluate-only early return below, so an
        # armed evaluate-only re-grade builds an inert (never-fed) watcher —
        # harmless, and keeps a single creation point.
        if early_stop_active(self.task):
            self._early_stop_watcher = EarlyStopWatcher.for_task(self.task)

        # Stage the reference BEFORE either branch returns: judge criteria with
        # include_reference=true (and any $REFERENCE_DIR/... file entry) expect it
        # populated in evaluate-only re-grades too, where no agent ever runs.
        await self._stage_reference()

        if self.sandbox is not None:
            # evaluate-only mode: sandbox already set up, skip agent
            assert self.result is not None
            self.sandbox.reference_dir = self._reference_dir
            self.result.sandbox_path = str(self.sandbox.sandbox_dir)

            self._resolve_routes()
            self._record_route_environment_info()
            return

        # Validate API keys (agent guaranteed non-None after experiment resolution).
        # validate_api_keys exempts the no-op agent (type: none) internally — it
        # makes no API call, so it needs no agent keys.
        assert self.task.agent is not None and self.task.agent.type is not None
        settings.validate_api_keys(str(self.task.agent.type))

        # Create sandbox with retry logic
        task_dir = self.task_file.parent.resolve() if self.task_file else None
        self.sandbox = Sandbox(self.task.sandbox, task_id=self.task.task_id, task_dir=task_dir)
        self.sandbox.reference_dir = self._reference_dir

        # workspace_dir (docker WORKDIR alignment) wins: run the agent in-place at
        # the image's own WORKDIR so its inputs/verifier paths line up, then copy
        # the workspace out to run_dir/artifacts in _cleanup. Otherwise:
        # DIRECT_WRITE runs the sandbox straight in run_dir/artifacts/<task_id>
        # (no end-of-run copy/move); MOVE_ON_WRITE / NONE run in a tempdir —
        # this keeps the run off run_dir on shared hosts, where parent-dir
        # node_modules can contaminate Node tool resolution (MST-9795).
        if self.workspace_dir is not None:
            if self.preservation_mode == PreservationMode.DIRECT_WRITE:
                logger.debug(
                    "workspace_dir=%s overrides DIRECT_WRITE; running in-place + capturing out.",
                    self.workspace_dir,
                )
            direct_target = self.workspace_dir
        elif self.preservation_mode == PreservationMode.DIRECT_WRITE:
            direct_target = self.run_dir / "artifacts" / self.task.task_id
        else:
            direct_target = None
        # DIRECT_WRITE deliberately does NOT clear the target dir, so a reused
        # --run-dir (or --resume) can leave a prior run's files alongside this
        # run's outputs and silently perturb file-based criteria. Surface it.
        # Skipped in workspace_dir mode: a WORKDIR (/root, /app) legitimately holds
        # the image's baked inputs — not stale prior-run files — so the warning
        # would fire on essentially every run and cry wolf.
        if (
            self.workspace_dir is None
            and direct_target is not None
            and direct_target.exists()
            and any(direct_target.iterdir())
        ):
            logger.warning(
                "DIRECT_WRITE target %s already exists and is non-empty; "
                + "stale files from a prior run are not cleared and may affect criteria.",
                direct_target,
            )

        async def _setup_sandbox() -> Any:
            assert self.sandbox is not None
            return await asyncio.to_thread(self.sandbox.setup, direct_target)

        sandbox_dir = await execute_with_retry(
            operation=_setup_sandbox,
            operation_name="Sandbox setup",
            context={"task_id": self.task.task_id, "component": "sandbox"},
        )

        # Assert result is initialized (set in run())
        assert self.result is not None, "Result not initialized"
        self.result.sandbox_path = str(sandbox_dir)

        # Determine API routing from settings.api_backend enum
        self._resolve_routes()

        # Create and start the agent. For a no-op (type: none) task this dispatches
        # to NoOpAgent, whose start/communicate/stop are no-ops — the orchestrator
        # runs the normal lifecycle without any agentless branching, and the
        # criteria are checked against the pre_run-prepared sandbox.
        assert self.task.agent is not None
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
            context={"task_id": self.task.task_id, "component": "agent", "agent_name": self._agent_name},
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

    def _eval_route_overrides(self) -> EvalRouteOverrides:
        """The ``(backend, model)`` pair from ``task.checker_context.api_route``, if any.

        A task/variant-authored choice for the WHOLE evaluation side (``llm_judge``,
        ``agent_judge``, the simulator all share one ``eval_route``), decoupled from
        the agent's own route/model — resolved into a credentialed ``ApiRoute`` (from
        env vars) by ``resolve_evaluation_route``, which bakes ``model`` into the
        resolved route's own ``model`` field. Reserved under one ``api_route``
        namespace (not per-criterion-type): there is exactly one eval route per run,
        not one per criterion. Either element is ``None`` when unset, in which case
        ``resolve_evaluation_route`` falls back to its existing pin-to-Claude /
        env-configured-default behavior. NOT currently ``-D``-reachable — task/variant
        YAML only.
        """
        api_route = self.task.checker_context.get("api_route", {})
        backend = api_route.get("route")
        model = api_route.get("model")
        return EvalRouteOverrides(
            backend=str(backend) if backend is not None else None,
            model=str(model) if model is not None else None,
        )

    def _resolve_routes(self) -> None:
        """Resolve ``self.route``/``self.eval_route``, log routing, and build
        ``self.success_checker``. Shared by the evaluate-only and normal setup
        paths in ``_setup`` — identical logic, previously duplicated at each call
        site. Requires ``self.sandbox`` to already be set.
        """
        assert self.sandbox is not None
        self.route = resolve_route(settings)
        overrides = self._eval_route_overrides()
        self.eval_route = resolve_evaluation_route(
            settings, self.route, backend_override=overrides.backend, model_override=overrides.model
        )
        logger.info("API routing: %s", _format_routing(self.route, self.task.agent.model if self.task.agent else None))
        self.success_checker = SuccessChecker(self.sandbox, route=self.eval_route)

    def _record_route_environment_info(self) -> None:
        """Persist resolved route + judge transport into ``result.environment_info``.

        Called from both the normal and evaluate-only setup paths so audit
        tooling sees identical keys regardless of run mode (catches the
        evaluate-only short-circuit before the recording block).
        """
        assert self.result is not None
        assert self.route is not None
        self.result.environment_info["api_routing"] = ROUTE_NAMES[type(self.route)]
        # The evaluation side (llm_judge / agent_judge / simulated user) may run on
        # a different, constant backend — pinned to Claude when the agent is on
        # LiteLLM — so record it: a run then shows what actually graded/simulated
        # it, distinct from the agent's api_routing.
        if self.eval_route is not None:
            self.result.environment_info["eval_routing"] = ROUTE_NAMES[type(self.eval_route)]
            # bedrock_model/litellm_model below are sourced from self.route (the
            # AGENT's route) — record the judge/simulator's own model separately so
            # a checker_context.api_route.model override (or the LiteLLM-agent
            # pinned-to-Bedrock default) is visible in run artifacts, not just
            # inferable from the agent's model.
            if self.eval_route.model:
                self.result.environment_info["eval_model"] = self.eval_route.model
        if isinstance(self.route, BedrockRoute):
            self.result.environment_info["aws_region"] = self.route.region
            if self.route.model:
                self.result.environment_info["bedrock_model"] = self.route.model
        elif isinstance(self.route, DirectRoute):
            # Record which transport llm_judge will use under DirectRoute so the
            # choice is visible in run artifacts (and not just the startup log).
            self.result.environment_info["judge_transport"] = self.route.judge_transport or "none"
        elif isinstance(self.route, LiteLLMRoute):
            # Host only (never the base_url or auth token) — mirrors the Codex
            # agent's host-only recording so secrets stay out of run artifacts.
            self.result.environment_info["litellm_base_url_host"] = urlparse(self.route.base_url).hostname or ""
            if self.route.model:
                self.result.environment_info["litellm_model"] = self.route.model
        # Agent-specific routing (e.g. Codex custom-endpoint / Azure). No-op for
        # the evaluate-only path (no agent) and for agents that add nothing.
        if self.agent is not None:
            self.result.environment_info.update(self.agent.get_environment_info())

    def _refresh_runtime_tool_versions(self) -> None:
        """Re-capture uip shell + tool-plugin versions after the task ran.

        ``get_version_info`` runs at setup time and therefore records the
        *pre-task* state. The UiPath CLI auto-installs/upgrades its
        ``@uipath/*-tool`` plugins on first use inside the task (under
        ``--driver docker`` containers start with image-baked or missing
        tools), so what the agent and criteria actually executed is only
        knowable once the task is done (coder_eval#366 follow-up). Resolve
        through the sandbox's agent-aligned PATH so we report the binary the
        task commands ran, not whichever ``uip`` this process sees first.

        The two keys update independently: a run can end with a post-task
        ``cli_version`` but setup-time ``tool_plugins`` (e.g. the plugin dir
        was lost to a PATH change while the shell still resolves) — each key
        keeps its setup-time value only when its own post-task resolution
        came back empty.

        Best-effort: skipped when the sandbox is gone; never raises.
        """
        if self.result is None or self.sandbox is None:
            return
        try:
            # Re-derive: auto-install may have (re)created the plugin dir mid-task.
            self.sandbox.refresh_plugin_tools_dir()
            versions = runtime_uip_versions(self.sandbox.plugin_tools_dir, self.sandbox.uip_search_path)
            # Keep the setup-time values when post-task resolution comes back
            # empty (e.g. `uip` gone from PATH) — they are the better estimate
            # of what the task ran than "unknown"/{}. Gate on looks_like_version
            # (not a "" / "unknown" denylist) so this sink shares the one
            # version-shape contract with _uip_version and the run-level join.
            if looks_like_version(versions.get("cli_version")):
                self.result.environment_info["cli_version"] = versions["cli_version"]
            if versions.get("tool_plugins"):
                self.result.environment_info["tool_plugins"] = versions["tool_plugins"]
        except Exception as exc:
            logger.debug("Failed to refresh runtime tool versions: %s", exc)

    async def _create_agent(self) -> Agent[Any]:
        """Create the appropriate agent based on task configuration.

        Uses the AgentRegistry factory to dispatch to registered agent implementations.

        Returns:
            Agent instance

        Raises:
            ValueError: If agent type is not supported
            TypeError: If config doesn't match agent's expected type
        """
        from coder_eval.agents import AgentRegistry, create_agent
        from coder_eval.plugins import ensure_plugins_loaded

        # Safety net for the production agent-construction path: create_agent no
        # longer self-loads (to keep plugins -> registry a one-way import edge), so
        # ensure plugin kinds are registered here before dispatch.
        ensure_plugins_loaded()
        assert self.task.agent is not None
        assert self.task.agent.type is not None
        # LiteLLM (open-weight) route only: give the agent correlation headers so a
        # proxy-side cost-logging callback can attribute each call's real cost +
        # cache buckets back to this task-run. x-ce-run-id is a stable per-task-run
        # key (the join, in _finalize_result, recomputes it identically); x-ce-task-id
        # is the human-readable canonical id.
        #
        # Gate on AGENT CAPABILITY, not the route: the route is settings-derived and
        # independent of agent type, but only agents whose __init__ accepts the kwarg
        # (supports_cost_log_tags) may receive it — otherwise the agent-agnostic
        # factory would forward it into NoOp/Codex/Antigravity/plugin constructors
        # that don't declare it and crash with TypeError under API_BACKEND=litellm.
        kwargs: dict[str, Any] = {}
        registration = AgentRegistry.get(self.task.agent.type)
        if (
            isinstance(self.route, LiteLLMRoute)
            and registration is not None
            and registration.agent_class.supports_cost_log_tags
        ):
            kwargs["cost_log_tags"] = {
                "x-ce-run-id": self._cost_correlation_run_id,
                "x-ce-task-id": self._log_task_id,
                "x-ce-attempt": self._cost_attempt_nonce,
            }
        return create_agent(self.task.agent.type, self.task.agent, route=self.route, **kwargs)

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

        # Compose the early-stop watcher into the stream when armed. It is the
        # sole callback when --stream is off, else it runs alongside the
        # TaskScopedCallback. The same watcher instance persists across retry
        # attempts (created once in _setup), so its turn/tool counters and
        # wall-clock origin accumulate correctly. The closures below read `watcher`.
        watcher = self._early_stop_watcher
        if watcher is not None:
            agent_callback = (
                CompositeStreamCallback([watcher, agent_callback]) if agent_callback is not None else watcher
            )

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
                should_stop=watcher.should_stop if watcher is not None else None,
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

        # ANTI-CHEAT WINDOW. The agent shares a filesystem with the harness, so
        # without this it can simply read the reference solution (and the task YAML
        # with its criteria) instead of solving the task. Both directories sit at
        # mode 000 for the whole of every communicate attempt — including retries,
        # since the wrapper is outside execute_with_retry — and are restored on
        # every exit path, so criteria and judges that run afterwards read normally.
        #
        # Routed through the SANDBOX, which owns whether a chmod window means
        # anything for its driver.
        #
        # task_dir is shielded ALONGSIDE reference_dir. It previously was not,
        # because under docker it was bind-mounted `:ro` and the chmod returned
        # EROFS -- producing only a per-turn "could not chmod" warning. It is now
        # a read-write throwaway copy (docker_runner._prepare_task_dir_mount), so
        # the window applies. That matters because the task dir holds grading
        # material beyond the reference: run_command fixtures, expected outputs,
        # and -- for a task laid out flat, whose parent is the whole `tasks/`
        # tree -- every SIBLING task's reference solution.
        #
        # What this does NOT do is hide the task DEFINITION. `task.yaml` is also
        # staged at /work/input for the in-container orchestrator to read, and
        # that mount is untouched by this window. Hiding the criteria from the
        # agent remains a separate, unsolved problem.
        assert self.sandbox is not None
        async with self.sandbox.set_permissions([self._reference_dir, self.sandbox.task_dir]):
            turn_record = await execute_with_retry(
                operation=_communicate_attempt,
                operation_name=operation_label,
                context={
                    "task_id": self.task.task_id,
                    "component": "agent",
                    "agent_name": self._agent_name,
                },
                on_attempt_error=_on_attempt_failure,
            )
        assert turn_record is not None  # execute_with_retry returns the turn or raises
        return turn_record

    @staticmethod
    def _accumulate_judge_usage(
        criteria_results: list[CriterionResult],
        accum: dict[tuple[int, str], TokenUsage],
    ) -> None:
        """Fold each turn's per-judge token usage into a dialog-wide running total.

        In simulation ``every_turn`` / ``both`` mode the orchestrator replaces
        ``success_criteria_results`` on every turn, and each fresh
        ``JudgeCriterionResult.token_usage`` carries only that turn's judge call
        (the snapshot/diff is taken per check). Without accumulation only the
        final turn's slice would survive on the persisted result. We keep a
        per-criterion running sum and rewrite each judge result's
        ``token_usage`` to the cumulative total so persisted billing reflects
        every judge call across the dialog.

        Ledger key is ``(position, criterion_type)`` — a stable criterion
        identity. ``check_all_async`` rebuilds ``success_criteria_results`` in the
        same order as ``task.success_criteria`` every turn (the positional
        alignment ``calculate_weighted_score`` enforces via ``zip(strict=True)``),
        so ``position`` is stable across turns; pairing it with the type makes
        the identity self-documenting and keeps two same-type judges distinct.
        """
        for i, r in enumerate(criteria_results):
            if not isinstance(r, JudgeCriterionResult):
                continue
            key = (i, r.criterion_type)
            prior = accum.get(key)
            turn_usage = r.token_usage
            if turn_usage is not None and not turn_usage.is_empty():
                combined = prior + turn_usage if prior is not None else turn_usage
                accum[key] = combined
                r.token_usage = combined
            elif prior is not None:
                # No fresh judge tokens this check — carry the running total
                # forward so it isn't dropped from the latest results list.
                r.token_usage = prior

    async def _evaluation_loop(self) -> bool:
        """Run the main evaluation loop.

        Returns:
            True if task succeeded, False otherwise
        """
        assert self.success_checker is not None, "Success checker not initialized"
        assert self.result is not None, "Result not initialized"
        # Every resolved task carries an agent config (no-op tasks resolve to a
        # NoneAgentConfig and run via NoOpAgent).
        assert self.task.agent is not None

        if self.agent is None:
            # No agent attached: evaluate-only re-grade of a completed sandbox.
            # (No-op tasks have a NoOpAgent here, so they take the normal path
            # below.) Check the criteria directly against the sandbox.
            assert self.success_checker is not None
            assert self.result is not None
            unsupported = [c.type for c in self.task.success_criteria if c.requires_agent]
            if unsupported:
                # Only reachable on the evaluate-only path (no agent attached):
                # re-grading a completed agent run whose trajectory is gone.
                logger.warning(
                    "Criteria %s require agent execution; results may be incomplete with no agent",
                    unsupported,
                )
            self.result.iteration_count = 1
            # Load reference in evaluate-only mode too: judge criteria with
            # include_reference=true expect this populated even when no agent
            # runs. The agent-driven branch below has the same call.
            criteria_results = await self.success_checker.check_all_async(
                self.task.success_criteria,
                reference_dir=self._reference_dir,
                turn_records=self.result.iterations,
            )
            self.result.success_criteria_results = criteria_results
            return self.result.all_criteria_passed(self.task.success_criteria)

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

        # initial_prompt is guaranteed for real agents (check_prompt_fields); a
        # no-op (type: none) task runs with no prompt — send empty, NoOpAgent
        # ignores it and returns an empty turn.
        current_prompt = self.task.initial_prompt or ""

        iteration = 1
        self.result.iteration_count = iteration

        # Communicate with agent (with retry logic)
        prompt_with_cwd = f"Your working directory is: {sandbox_dir.resolve()}\n\n{current_prompt}"
        logger.debug(f"Sending prompt: {current_prompt[:100]}...")

        # The agent owns its lifecycle events now (AgentStartEvent fires inside
        # communicate()); the orchestrator is a pure consumer of the stream.
        turn_record = await self._communicate_with_retry(
            prompt=prompt_with_cwd,
            iteration=iteration,
            operation_label="Agent communication",
        )
        self.result.iterations.append(turn_record)
        self._sync_sandbox_command_path_with_agent()

        # Record early-stop info (if the watcher tripped) BEFORE check_all_async, so it
        # survives even if a checker raises. None on a full run or when unarmed.
        self.result.early_stop = self._early_stop_watcher.info if self._early_stop_watcher is not None else None

        logger.debug(f"Agent response received ({len(turn_record.agent_output)} chars)")

        # Check success criteria (reference_dir feeds reference_comparison + judges)
        logger.debug("Checking success criteria")
        await self._verify_reference_integrity()
        criteria_results = await self.success_checker.check_all_async(
            self.task.success_criteria,
            reference_dir=self._reference_dir,
            turn_records=self.result.iterations,
        )
        self.result.success_criteria_results = criteria_results

        # Determine if all criteria passed their thresholds. all_passed is
        # single-sourced via the model gate; passed_count/total_count are kept
        # only for the human-readable log line below.
        pairs = list(zip(criteria_results, self.task.success_criteria, strict=True))
        passed_count = sum(1 for r, c in pairs if r.score >= c.pass_threshold)
        total_count = len(pairs)
        # Gate selection is FIRED-ONLY: the weighted armed gate applies iff the
        # watcher actually cut the run (early_stop is not None) — on a truncated
        # trajectory the unarmed criteria never had the chance to be satisfied,
        # so they stay advisory. A run that completed naturally (armed or not,
        # watcher never fired or disarmed fail-open) has a full trajectory and
        # gates strict-AND over every gating criterion, exactly like an unarmed
        # run — arming a criterion (e.g. adding a decide_within fail-fast
        # timeout) must never change the verdict of a run it didn't cut.
        if self.result.early_stop is not None:
            # One gate for every early-stopped run, no per-reason branches: a
            # decision-budget stop is just a fail-stop whose deciding criterion
            # timed out (the watcher only fires once the weighted ceiling
            # proves the armed gate cannot pass). The ceiling is an upper bound
            # on the authoritative armed score only because the watcher reduces
            # the SAME trajectory the checker scores — it records UNRESOLVED
            # tool ends exactly like the agent's EventCollector does (see
            # EarlyStopWatcher._on_event_impl) — so the weighted armed gate is
            # correct whether the watcher fired on a pass, a fail, or a timeout.
            gate_threshold = (
                self.task.run_limits.stop_early_gate_threshold
                if self.task.run_limits is not None
                else DEFAULT_STOP_EARLY_GATE_THRESHOLD
            )
            all_passed = self.result.armed_criteria_passed(self.task.success_criteria, gate_threshold)
            armed_count = sum(1 for c in self.task.success_criteria if c.is_stop_armed)
            logger.info(
                "Early-stopped run (%s): gating on %d armed criteria (%d advisory, not gated).",
                self.result.early_stop.reason.value,
                armed_count,
                total_count - armed_count,
            )
        else:
            if self._early_stop_watcher is not None:
                if self._early_stop_watcher.disarmed:
                    logger.info("early-stop watcher disarmed fail-open (verdict error): gating on the full set.")
                else:
                    logger.info("early-stop armed but never fired (run completed naturally): gating on the full set.")
            all_passed = self.result.all_criteria_passed(self.task.success_criteria)

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

    def _build_simulation_telemetry(
        self,
        *,
        n_trials: int,
        stop_reason: DialogStopReason,
        total_turns: int,
        sim_in: int,
        sim_out: int,
        sim_failures: int,
        sim_model: str | None = None,
    ) -> SimulationTelemetry:
        """Single construction point for SimulationTelemetry across the dialog loop's exit paths."""
        return SimulationTelemetry(
            n_trials=n_trials,
            replicate_index=self.replicate_index,
            stop_reason=stop_reason.value,
            simulator_input_tokens=sim_in,
            simulator_output_tokens=sim_out,
            simulator_failures=sim_failures,
            total_turns=total_turns,
            # The resolved id, not the configured one, so the record names the model
            # the backend actually served and simulator cost prices from a fact.
            simulator_model=sim_model,
        )

    async def _run_dialog_criteria_check(
        self, judge_usage_accum: dict[tuple[int, str], TokenUsage]
    ) -> list[CriterionResult]:
        """Run the success criteria against the current dialog state and record them.

        The block lifted verbatim from the three identical sites (per-turn,
        budget-gate fallback, end-of-dialog): (re)load the reference, run
        ``check_all_async``, fold this turn's judge usage into the
        dialog-wide accumulator, store the results, and recompute the weighted score.
        Whether to additionally set ``all_passed`` and emit a ``CriteriaCheckEvent`` is
        left to the caller — those differ across the sites (the budget-gate fallback
        does neither).
        """
        assert self.result is not None
        assert self.success_checker is not None
        await self._verify_reference_integrity()
        criteria_results = await self.success_checker.check_all_async(
            self.task.success_criteria,
            reference_dir=self._reference_dir,
            turn_records=self.result.iterations,
        )
        self._accumulate_judge_usage(criteria_results, judge_usage_accum)
        self.result.success_criteria_results = criteria_results
        self.result.calculate_weighted_score(self.task.success_criteria)
        return criteria_results

    def _user_message_from_sim(
        self,
        sim_result: SimulatorResult,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: float,
        model_id: str | None,
    ) -> UserMessage:
        """Map a ``SimulatorResult`` + timing onto the pending-user-turn ``UserMessage``.

        Distinct from the trivial pinned-prompt ``UserMessage(text=initial_prompt)``,
        which stays inline. Isolated as a named, independently-testable field mapping.
        """
        return UserMessage(
            text=sim_result.text,
            raw_text=sim_result.raw_text,
            stop_requested=sim_result.stop_requested,
            started_at=started_at,
            completed_at=completed_at,
            generation_duration_ms=duration_ms,
            input_tokens=sim_result.input_tokens or 0,
            output_tokens=sim_result.output_tokens or 0,
            model=model_id,
        )

    async def _solicit_user_message(
        self,
        simulator: UserSimulator,
        history: list[tuple[str, str]],
        sim_model_id: str | None,
        failure_log_msg: str,
    ) -> _SolicitedMessage:
        """Ask the simulator for one utterance, time it, and map it to a ``UserMessage``.

        The DRY unification of the opener and the in-loop next-user-message sites, which
        are structurally identical apart from the failure log string. Carries NO
        control-flow decision: on a simulator exception it logs ``failure_log_msg`` and
        returns ``message=None`` (with ``sim_in=sim_out=0``); callers inspect
        ``message is None`` (failure) and ``message.stop_requested``, and fold
        ``sim_in``/``sim_out`` into their running totals.
        """
        started_wall = datetime.now()
        started_mono = time.monotonic()
        try:
            sim_result = await simulator.next_user_message(history)
        except Exception:
            logger.exception(failure_log_msg)
            return _SolicitedMessage(message=None, sim_in=0, sim_out=0)
        completed_wall = datetime.now()
        duration_ms = (time.monotonic() - started_mono) * 1000
        message = self._user_message_from_sim(sim_result, started_wall, completed_wall, duration_ms, sim_model_id)
        return _SolicitedMessage(
            message=message,
            sim_in=sim_result.input_tokens or 0,
            sim_out=sim_result.output_tokens or 0,
        )

    async def _acquire_opener(
        self, simulator: UserSimulator, sim_config: SimulationConfig, sim_model_id: str | None
    ) -> _OpenerOutcome:
        """Pure-simulation opener (``initial_prompt is None``): ask the simulator to
        generate turn 1 from empty history.

        Two short-circuit paths write simulation telemetry inline and tell the caller to
        return immediately: a simulator failure (ERROR, total_turns=0) and an opener that
        already carries the stop token (STOP_TOKEN, total_turns=0 — running an agent turn
        just to learn this wastes a turn budget). Otherwise returns the opener message and
        its token deltas for the caller to fold in.
        """
        assert self.result is not None
        solicited = await self._solicit_user_message(
            simulator,
            [],
            sim_model_id,
            "User simulator failed to generate opening message — aborting dialog",
        )
        if solicited.message is None:
            self.result.simulation = self._build_simulation_telemetry(
                n_trials=sim_config.n_trials,
                stop_reason=DialogStopReason.ERROR,
                total_turns=0,
                sim_in=0,
                sim_out=0,
                sim_failures=1,
                sim_model=sim_model_id,
            )
            return _OpenerOutcome(short_circuit=True, return_value=False)

        self._log_conversation("USER", 1, solicited.message.text, metadata="simulator-generated opener")

        # Opener carrying the stop token means the simulator judged the task done
        # before any agent turn ran. Record telemetry and short-circuit.
        if solicited.message.stop_requested:
            self.result.simulation = self._build_simulation_telemetry(
                n_trials=sim_config.n_trials,
                stop_reason=DialogStopReason.STOP_TOKEN,
                total_turns=0,
                sim_in=solicited.sim_in,
                sim_out=solicited.sim_out,
                sim_failures=0,
                sim_model=sim_model_id,
            )
            return _OpenerOutcome(short_circuit=True, return_value=False)

        return _OpenerOutcome(
            short_circuit=False,
            current_prompt=solicited.message.text,
            pending_user_turn=solicited.message,
            sim_in=solicited.sim_in,
            sim_out=solicited.sim_out,
            failures=0,
        )

    async def _simulation_dialog_loop(self, initial_prompt: str | None, sandbox_dir: Path) -> bool:  # noqa: PLR0915 — sequential dialog driver; decomposed into helpers, residual length is irreducible without a _DialogState rewrite (see plan Phase 5). Statement count ratcheted by CE022.
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

        Emits the same streaming events as the single-shot loop (the agent emits
        its ``AgentStart``/``Turn``/``Tool``/``AgentEnd`` lifecycle; the orchestrator
        adds ``CriteriaCheckEvent``) so downstream UI renderers work unchanged.
        Simulator telemetry is recorded on ``self.result.simulation``.
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
            # Pin the simulated user to the constant Claude eval route, not the
            # agent's (possibly open-weight) route, so the simulator behaves
            # identically across the models under test.
            route=self.eval_route,
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
        # The RESOLVED simulator model (backend-translated), not the configured id —
        # it labels each simulator UserMessage and is persisted on the telemetry so
        # simulator cost prices from the model that actually served the call.
        sim_model_id = simulator.model
        # Track whether we entered the agent-call loop — used by the finally
        # block to decide whether to persist an orphaned pending_user_turn.
        agent_turn_attempted = False

        try:
            # Pure-simulation mode: no pinned opener — ask the simulator to
            # generate turn 1 from empty history before the agent sees anything.
            if initial_prompt is None:
                opener_outcome = await self._acquire_opener(simulator, sim_config, sim_model_id)
                if opener_outcome.short_circuit:
                    # _acquire_opener already wrote the simulation telemetry (ERROR
                    # on simulator failure, STOP_TOKEN on a stop-carrying opener).
                    return opener_outcome.return_value
                assert opener_outcome.current_prompt is not None
                simulator_input_tokens += opener_outcome.sim_in
                simulator_output_tokens += opener_outcome.sim_out
                total_tokens_used += opener_outcome.sim_in + opener_outcome.sim_out
                simulator_failures += opener_outcome.failures
                current_prompt = opener_outcome.current_prompt
                pending_user_turn = opener_outcome.pending_user_turn
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

            # Dialog-wide running total of each judge's token usage. The per-turn
            # check replaces ``success_criteria_results`` wholesale, so we fold
            # each turn's judge slice into this accumulator (see
            # ``_accumulate_judge_usage``) to avoid dropping earlier judge calls.
            # Keyed by (position, criterion_type) — a stable criterion identity.
            judge_usage_accum: dict[tuple[int, str], TokenUsage] = {}

            # turns_completed advances in lockstep with the agent's _iteration:
            # one _communicate_with_retry call per sim turn keeps partials
            # and the successful retry on the same iteration number.
            while True:
                turns_completed += 1
                self.result.iteration_count = turns_completed
                logger.info("Simulation turn %s/%s", turns_completed, sim_config.max_turns)

                prompt_with_cwd = f"Your working directory is: {sandbox_dir.resolve()}\n\n{current_prompt}"
                # Agent emits its own AgentStartEvent inside communicate().
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
                    if turn_record.token_usage.uncached_input_tokens:
                        usage_parts.append(f"in={turn_record.token_usage.uncached_input_tokens}")
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

                if turn_record.token_usage is not None:
                    usage = turn_record.token_usage
                    total_tokens_used += (usage.uncached_input_tokens or 0) + (usage.output_tokens or 0)

                criteria_checked_this_turn = False
                if check_every_turn:
                    criteria_results = await self._run_dialog_criteria_check(judge_usage_accum)
                    criteria_checked_this_turn = True
                    all_passed = self.result.all_criteria_passed(self.task.success_criteria)
                    self._emit_criteria_event(criteria_results)

                # Budget gate: aborts the dialog with a dedicated stop reason and
                # ensures end-of-dialog criteria still run for partial credit.
                try:
                    self._check_run_limits(iteration=turns_completed)
                except BudgetExceededError:
                    stop_reason = DialogStopReason.RUN_LIMIT_EXCEEDED
                    if not criteria_checked_this_turn:
                        # Run for partial-credit side effects only (stores results
                        # on self.result + recomputes score). This fallback site
                        # deliberately neither sets all_passed nor emits an event,
                        # so the return value is unused.
                        await self._run_dialog_criteria_check(judge_usage_accum)
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

                solicited = await self._solicit_user_message(
                    simulator, dialog_pairs, sim_model_id, "User simulator failed — ending dialog"
                )
                if solicited.message is None:
                    simulator_failures += 1
                    stop_reason = DialogStopReason.ERROR
                    break

                simulator_input_tokens += solicited.sim_in
                simulator_output_tokens += solicited.sim_out
                total_tokens_used += solicited.sim_in + solicited.sim_out

                pending_user_turn = solicited.message

                if solicited.message.stop_requested:
                    self._log_conversation(
                        "USER",
                        turns_completed + 1,
                        solicited.message.text,
                        metadata="stop token — dialog ending",
                    )
                    stop_reason = DialogStopReason.STOP_TOKEN
                    break

                current_prompt = solicited.message.text
                self._log_conversation("USER", turns_completed + 1, current_prompt)

            # Final criteria check for end_of_dialog (or both) modes, or when
            # every_turn mode did not run a check for the final turn.
            end_of_dialog_needed = (
                sim_config.check_criteria in ("end_of_dialog", "both") or not criteria_checked_this_turn
            )
            if end_of_dialog_needed:
                criteria_results = await self._run_dialog_criteria_check(judge_usage_accum)
                all_passed = self.result.all_criteria_passed(self.task.success_criteria)
                self._emit_criteria_event(criteria_results)

            assert stop_reason is not None, "dialog loop exited without picking a stop_reason"
            self.result.simulation = self._build_simulation_telemetry(
                n_trials=sim_config.n_trials,
                stop_reason=stop_reason,
                total_turns=turns_completed,
                sim_in=simulator_input_tokens,
                sim_out=simulator_output_tokens,
                sim_failures=simulator_failures,
                sim_model=sim_model_id,
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
                self.result.simulation = self._build_simulation_telemetry(
                    n_trials=sim_config.n_trials,
                    stop_reason=stop_reason or DialogStopReason.ERROR,
                    total_turns=turns_completed,
                    sim_in=simulator_input_tokens,
                    sim_out=simulator_output_tokens,
                    sim_failures=simulator_failures,
                    sim_model=sim_model_id,
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

            # Bound outside the try so the `finally` can tell "never spawned"
            # (a create_subprocess_shell failure) from "spawned, needs closing".
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd.command,
                    cwd=str(sandbox_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self._POST_RUN_STREAM_LIMIT,
                )
                # No `# nosec` here: bandit does not flag
                # asyncio.create_subprocess_shell at all (B602 is
                # subprocess.Popen(shell=True)), so the suppression this line
                # used to carry was inert -- and an inert id silently
                # pre-suppresses a real finding if a flagged construct is ever
                # added here. The shell IS intentional: pre/post_run commands are
                # authored in the task YAML, which is already a trusted artifact
                # (it can run anything via a run_command criterion).

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
            finally:
                # Every exit from this iteration -- normal, `continue` from the
                # timeout branch, or a raised RuntimeError -- must release the
                # two pipes this command opened. Without it they survive until
                # GC, which on Windows reports as an unraisable ResourceWarning
                # against an unrelated test. See _close_subprocess_transport.
                _close_subprocess_transport(proc)

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

        # Drop the staged reference copy. Deliberately NOT preserved into
        # run_dir/artifacts: run directories get archived, uploaded, and shared,
        # and the reference solution must not ride along.
        #
        # Keyed on _reference_staging_root, recorded before the copy — NOT on
        # _reference_dir.parent, which is only set once the copy succeeds and so
        # would leak a half-written reference when copytree raises. The field is
        # None under docker, where the reference is the host-owned bind mount:
        # that one is not ours to delete, and rmtree'ing its parent would take
        # /work with it.
        #
        # rmtree_restrictive, not rmtree(ignore_errors=True): a run killed
        # mid-turn leaves the tree at mode 000, where scandir raises
        # PermissionError and plain rmtree silently declines — orphaning a
        # tempdir that holds the reference solution, with no log line.
        staging_root = self._reference_staging_root
        self._reference_dir = None
        self._reference_staging_root = None
        if staging_root is not None:
            try:
                await asyncio.to_thread(rmtree_restrictive, staging_root)
            except Exception as e:
                logger.warning("Failed to remove staged reference dir %s: %s", staging_root, e)

        # Cleanup sandbox. Preservation and cleanup() are SIBLING try blocks:
        # a preservation failure (e.g. disk full during preserve_to) must never
        # skip cleanup(), or the tempdir leaks.
        if self.sandbox:
            try:
                if self.workspace_dir is not None and self.result:
                    # Docker WORKDIR alignment: the agent ran in-place at the image
                    # WORKDIR; copy that workspace out to run_dir/artifacts/<task>
                    # (capture_to grants cross-uid read on the COPY and tolerates
                    # dangling symlinks). Takes precedence over preservation_mode.
                    artifacts_dir = self.run_dir / "artifacts"
                    preserved_path = await asyncio.to_thread(self.sandbox.capture_to, artifacts_dir)
                    self.result.sandbox_path = str(preserved_path)
                    logger.info("Workspace captured out: %s -> %s", self.workspace_dir, preserved_path)
                elif self.preservation_mode == PreservationMode.MOVE_ON_WRITE and self.result:
                    # Sandbox ran in a tempdir — move it into run_dir/artifacts.
                    artifacts_dir = self.run_dir / "artifacts"
                    preserved_path = await asyncio.to_thread(self.sandbox.preserve_to, artifacts_dir)
                    self.result.sandbox_path = str(preserved_path)
                    logger.info(f"Sandbox preserved to: {preserved_path}")
                elif self.preservation_mode == PreservationMode.DIRECT_WRITE and self.result:
                    # Sandbox already lives in run_dir/artifacts — nothing to move.
                    # Set sandbox_path first, then grant a+rX (a fallible chmod) so
                    # artifacts written by a root-owned docker container stay
                    # traversable across the host uid boundary (MOVE_ON_WRITE gets
                    # this via preserve_to; DIRECT_WRITE skips it, so apply it here).
                    self.result.sandbox_path = str(self.sandbox.sandbox_dir)
                    await asyncio.to_thread(self.sandbox.grant_read_access)
                    logger.info(f"Sandbox preserved (in-place): {self.sandbox.sandbox_dir}")
                elif self.preservation_mode == PreservationMode.NONE and self.result:
                    # Sandbox will be deleted by cleanup() below; clear stale path.
                    self.result.sandbox_path = None
                elif self.result:
                    # Defensive: a future PreservationMode member with no arm here
                    # would otherwise silently fall through. Treat as no-preserve.
                    logger.warning("Unhandled preservation_mode %s; not preserving sandbox.", self.preservation_mode)
                    self.result.sandbox_path = None
            except Exception as e:
                logger.warning(f"Failed to preserve sandbox (continuing with cleanup): {e}")
                if self.result and self.preservation_mode == PreservationMode.MOVE_ON_WRITE:
                    # The artifacts were never moved — don't point at the tempdir
                    # cleanup() is about to delete. DIRECT_WRITE keeps its path:
                    # that sandbox is persistent (cleanup() is a no-op) and the
                    # artifacts still exist even if the a+rX chmod failed.
                    self.result.sandbox_path = None

            try:
                # cleanup() is a no-op for persistent dirs (is_persistent=True)
                await asyncio.to_thread(self.sandbox.cleanup, preserve=False)
            except Exception as e:
                logger.warning(f"Failed to cleanup sandbox: {e}")
