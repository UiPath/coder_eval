"""Success criteria models for task evaluation."""

# Each concrete criterion narrows the base ``type: str`` to its ``Literal[...]`` tag
# (the standard pydantic discriminated-union pattern). Pyright treats a Literal
# subtype override of a mutable str field as variable-invariant-incompatible, but
# the narrowing is exactly the intended discriminator behaviour here.
# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coder_eval.models.agent_config import AgentConfig, ClaudeCodeAgentConfig, parse_agent_config
from coder_eval.models.enums import AgentKind
from coder_eval.models.judge_defaults import DEFAULT_JUDGE_MODEL


# SECURITY: ignore_patterns floor. The judge's working directory is a copy of
# the agent's sandbox; if the agent dropped these in, they'd otherwise be
# copied across and either (a) cause the SDK to load hooks / MCP servers from
# the main agent's settings (despite ``setting_sources=[]``, the agent ignore
# patterns also gate what shutil.copytree pulls in) or (b) collide with the
# reference mount point. The floor is enforced UNCONDITIONALLY in
# ``criteria/agent_judge.py::_build_agent_config``, even when the user
# supplied their own ``agent.ignore_patterns`` — author convenience does not
# override the judge's safety floor. Single source of truth: importing this
# constant from both call-sites keeps the model defaults and the checker
# floor in sync.
JUDGE_SECURITY_IGNORE_FLOOR: tuple[str, ...] = (".claude", ".mcp.json", "_reference")


def _reject_removed_verdict_channel(data: Any) -> Any:
    """Raise with a migration message when YAML still carries the removed ``verdict_channel`` key.

    Shared between ``LLMJudgeCriterion`` and ``AgentJudgeCriterion`` so the error
    string stays in lockstep. Runs in ``mode="before"`` so the migration message
    wins over the generic ``extra="forbid"`` "Extra inputs are not permitted"
    error.
    """
    if isinstance(data, dict) and "verdict_channel" in data:
        raise ValueError(
            "verdict_channel field was removed on 2026-05-21; the submit_verdict "
            + "tool channel is the only path. Delete this field from your task YAML."
        )
    return data


def _default_judge_agent_config() -> ClaudeCodeAgentConfig:
    """Build the default judge AgentConfig.

    The returned ``ignore_patterns`` extend ``JUDGE_SECURITY_IGNORE_FLOOR``
    with developer-noise dirs (``.git``, ``node_modules``, ...). The security
    trio is appended via the shared constant so it cannot drift from the
    floor enforced in ``_build_agent_config``.
    """
    # Developer-noise dirs first, security floor (single source of truth) last.
    developer_noise = [".git", "node_modules", "__pycache__", ".venv"]
    cfg = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        model="claude-sonnet-4-6",
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "Read", "Glob", "Grep"],
        ignore_patterns=[*developer_noise, *JUDGE_SECURITY_IGNORE_FLOOR],
    )
    assert isinstance(cfg, ClaudeCodeAgentConfig)
    return cfg


class BaseSuccessCriterion(BaseModel, ABC):
    """Base class for success criteria - pure data container.

    Success criteria are data models that describe WHAT to check,
    but not HOW to check it. The checking logic belongs in SuccessChecker.

    This separation provides:
    - Models focus on data + validation (Pydantic's strength)
    - SuccessChecker focuses on business logic
    - Easier testing with mocked sandboxes
    - Better separation of concerns
    - No circular dependency with Sandbox class

    ``extra='forbid'`` here propagates to every concrete criterion subclass via
    pydantic's model_config inheritance. A typo in any criterion-specific field
    in a task YAML (e.g. ``patten:`` instead of ``pattern:``) surfaces as an
    explicit ``Extra inputs are not permitted`` error rather than being silently
    dropped on the floor.
    """

    model_config = ConfigDict(extra="forbid")

    # Discriminator; each concrete criterion narrows this to its Literal tag.
    type: str

    description: str = Field(description="Human-readable description of what this criterion checks")

    weight: float = Field(
        default=1.0,
        ge=0.0,
        allow_inf_nan=False,
        description=(
            "Relative importance of this criterion in the weighted score (default: 1.0). Set to 0 to "
            "make the criterion purely INFORMATIONAL -- useful for side-effect checks (e.g. a setup "
            "command): it is excluded from the weighted score AND from the pass/fail gate, so scoring "
            "below its pass_threshold no longer flips the task to FAILURE. The result is still "
            "computed, stored, and rendered in reports. A weight=0 criterion may not set a stop_early "
            "block or suite_thresholds (arming a non-gating criterion for a pass/fail gate is "
            "incoherent)."
        ),
    )

    pass_threshold: float = Field(
        default=0.9, ge=0.0, le=1.0, description="Minimum score required to pass (default: 0.9 = 90%)"
    )

    suite_thresholds: dict[str, float] | None = Field(
        default=None,
        description=(
            "Across-row thresholds as {metric_name: minimum}. Only valid on tasks that declare a dataset:. "
            "The criterion passes at the suite level iff every listed metric meets its minimum. "
            "Metric names come from the criterion's aggregate() output (e.g. 'accuracy', 'f1.macro', "
            "'recall.positive' for classification_match)."
        ),
    )

    requires_agent: ClassVar[bool] = False
    """True if this criterion requires agent turn records to evaluate correctly."""

    @property
    def is_stop_armed(self) -> bool:
        """True when this criterion participates in the run's early-stop armed set.

        Always ``False`` on the base: only live-observable criteria
        (``LiveSuccessCriterion`` subclasses) carry stop triggers, so arming an
        unobservable criterion is unrepresentable rather than a validation
        error. The armed set drives both the runtime watcher
        (``EarlyStopWatcher``) and the weighted armed gate
        (``EvaluationResult.armed_criteria_passed``).
        """
        return False

    def model_post_init(self, context: Any, /) -> None:
        # Pin the discriminator tag into model_fields_set so it survives
        # model_dump(exclude_unset=True) → model_validate() round-trips even for
        # directly-constructed criteria (where the tag comes from the Literal
        # default, not the caller). Without this, exclude_unset drops the tag and
        # the discriminated union rejects the dump with union_tag_not_found.
        self.__pydantic_fields_set__.add("type")

    @model_validator(mode="after")
    def check_weight_zero_is_not_gating(self) -> Self:
        """Reject ``weight: 0`` combined with any gate-arming field.

        ``weight: 0`` makes a criterion informational — excluded from the score
        and from the pass/fail gate (see ``is_gating``). Both the early-stop
        triggers (``is_stop_armed``: arms the per-row early-stop gate) and
        ``suite_thresholds`` (arms the across-row suite gate, which drives the
        run's exit code) would let an "informational" criterion flip a run to
        failure — directly contradicting the field's contract. So both
        combinations are authoring errors, caught at load time rather than
        surfacing as a confusing exit code later.
        """
        if self.weight == 0.0:
            if self.is_stop_armed:
                raise ValueError(
                    f"criterion {self.type!r}: weight=0 makes the criterion informational (non-gating), "
                    + "so it cannot also set a stop_early block, which arms it for a pass/fail gate. "
                    + "Give it a non-zero weight, or drop the block."
                )
            if self.suite_thresholds is not None:
                raise ValueError(
                    f"criterion {self.type!r}: weight=0 makes the criterion informational (non-gating), "
                    + "so it cannot also set suite_thresholds (which arms it for a pass/fail gate). "
                    + "Give it a non-zero weight, or drop suite_thresholds."
                )
        return self

    @property
    def is_gating(self) -> bool:
        """True when this criterion participates in the task's pass/fail gate.

        Single source of truth for "does a low score here fail the task". A
        ``weight: 0`` criterion is informational: it is excluded from the
        weighted score (it contributes 0 to both numerator and denominator) and,
        by the same token, from the gate — so the two never disagree.
        """
        return self.weight > 0.0

    # Business logic (check operations) moved to SuccessChecker in evaluator.py


# The two polarities a live-observable criterion can decide mid-run — distinct
# from the 3-value LiveVerdict ("pass"/"fail"/"undecided") the checker's
# live_verdict returns: this is the narrower CAPABILITY type, "undecided" is
# never a valid decidable polarity. Typed here (not a bare frozenset[str]) so
# a live_decidable_polarities override returning a stray/typo'd string, or
# "undecided" itself, is a pyright error rather than a runtime-only lint gap.
LivePolarity = Literal["pass", "fail"]


class StopEarlyPolicy(BaseModel):
    """Per-criterion early-stop policy — presence of the block IS the arming.

    Attaching ``stop_early:`` to a live-observable criterion arms it for the
    run's early-stop watcher — there is no run-level master switch; the block
    alone activates the watcher (``run_limits.stop_early: false`` is the
    run-level veto). Arming carries ONE implicit trigger — a definitive *effective* fail
    (a native live-fail, or the ``decide_within`` timeout expiring) may end the
    run under the weighted ceiling rule — plus the two knobs below. A trigger
    whose polarity the instance can never decide is inert by design, so one
    dataset-fanned YAML line (same block on every row) serves both positive
    rows (pass/timeout live) and distractor rows (fail live).
    """

    model_config = ConfigDict(extra="forbid")

    on_pass: Literal["stop", "continue"] = Field(
        default="continue",
        description=(
            "What a live PASS does. 'continue' (default): the verdict latches (the criterion is "
            "not re-checked) and the run proceeds untouched — use with decide_within for a "
            "fail-fast timeout that does not cut successful runs short. 'stop': end the run (a "
            "pass-stop) the moment this criterion live-passes — subject to the weighted floor "
            "rule: the stop fires only once the on_pass=stop subset's worst-case weighted score "
            "already meets run_limits.stop_early_gate_threshold. Inert on an instance that can "
            "never live-pass (e.g. a distractor row)."
        ),
    )

    decide_within: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Timeout in tool-call steps: still 'undecided' after this many steps latches an "
            "effective FAIL — fed through the same weighted ceiling fail-stop rule as a native "
            "live-fail, reported as reason 'decision_budget_exceeded'. Inert on an instance "
            "that can only ever live-fail (a distractor/guard, whose 'undecided' is its "
            "success state). None (default) = no timeout. The step count is CUMULATIVE across "
            "every retry attempt of the turn (the same EarlyStopWatcher instance, and its "
            "counters, persist across retries) — including attempts that ultimately crashed or "
            "timed out before this criterion's own investigation even began. Size it with that "
            "headroom in mind."
        ),
    )


class LiveSuccessCriterion(BaseSuccessCriterion):
    """Base for criteria observable from a PARTIAL, mid-run trajectory (early-stop).

    ``live_decidable_polarities`` is a pure function of THIS instance's own
    fields — no ``turn_records``, no sandbox, no checker instance needed (e.g.
    ``command_executed`` can decide this purely from whether ``max_count`` is
    set). That makes it genuinely computable on the data model rather than the
    checker, unlike the checker's ``live_verdict`` (``criteria/base.py``),
    which reads the actual trajectory and stays checker-side logic. Moving
    decidability here also gives early-stop-only config (the
    ``stop_early`` block) a home that doesn't pollute ``BaseSuccessCriterion``
    with a field meaningless for every non-observable criterion type.

    Only ``SkillTriggeredCriterion`` / ``CommandExecutedCriterion`` subclass
    this today; a criterion type is "live-observable" iff it is a
    ``LiveSuccessCriterion`` subclass — the single source of truth
    ``validate_early_stop`` / ``EarlyStopWatcher`` consult (no separate
    checker-side flag to keep in sync).
    """

    stop_early: StopEarlyPolicy | None = Field(
        default=None,
        description=(
            "Opt-in early-stop policy block; its PRESENCE arms this criterion for the run's "
            "early-stop watcher — the block alone activates the watcher, there is no run-level "
            "master switch (run_limits.stop_early: false is the run-level veto). An armed "
            "criterion's definitive effective FAIL — a native live-fail, or the decide_within "
            "timeout expiring — may end the run under the weighted ceiling rule (deferred "
            "while any pass-capable armed criterion is still undecided); set on_pass: stop to "
            "also end the run on a live PASS. An empty block (stop_early: {}) is the idiomatic "
            "distractor arming: fail-stop on misfire, nothing else. Triggers whose polarity "
            "this instance cannot decide are inert by design (dataset fan-out support). "
            "Unarmed criteria stay advisory on an early-stopped run. Only exists on "
            "live-observable criteria, so arming anything else is a schema error."
        ),
    )

    @property
    def is_stop_armed(self) -> bool:
        """True when the ``stop_early:`` block is present on this instance.

        Presence of the block IS the arming — the implicit fail trigger comes
        with it, ``on_pass`` / ``decide_within`` refine it. A trigger whose
        polarity this instance can never decide is inert by design (see
        ``StopEarlyPolicy``) — that is what lets one dataset-fanned YAML
        line serve both positive and distractor rows without per-row
        conditionals.
        """
        return self.stop_early is not None

    @abstractmethod
    def live_decidable_polarities(self) -> frozenset[LivePolarity]:
        """Which polarities THIS instance can decide mid-run, from its own fields alone.

        Must return a subset of the polarities the corresponding checker's
        ``live_verdict`` can ever emit for this criterion type. Used by
        ``EarlyStopWatcher`` to decide which triggers of an armed criterion's
        ``stop_early`` block are live for this instance: ``on_pass: stop``
        needs ``"pass"``, the implicit fail trigger needs ``"fail"``, and
        ``decide_within`` needs ``"pass"`` (a fail-only instance's 'undecided'
        is its success state, so the timeout is inert there). A trigger whose
        polarity is missing from this set is inert by design, not an error —
        that is what lets one dataset-fanned YAML line serve both positive and
        distractor rows.
        """


class FileExistsCriterion(BaseSuccessCriterion):
    """Check if a file exists at the specified path.

    Pure data model - checking logic in SuccessChecker._check_file_exists()
    """

    type: Literal["file_exists"] = "file_exists"
    path: str = Field(description="Path to the file that must exist")


class FileContainsCriterion(BaseSuccessCriterion):
    """Check if a file contains specific strings.

    Pure data model - checking logic in SuccessChecker._check_file_contains()
    """

    type: Literal["file_contains"] = "file_contains"
    path: str = Field(description="Path to the file to check")
    includes: list[str] = Field(description="List of strings that must be present in the file")
    excludes: list[str] | None = Field(default=None, description="List of strings that must NOT be present in the file")


class RunCommandCriterion(BaseSuccessCriterion):
    """Check if a command runs successfully, with optional stdout matching.

    When ``expected_stdout`` is set, the criterion also compares the command's
    stdout against the expected value using the mode specified by ``stdout_match``.
    This subsumes the former ``program_stdout_equals`` criterion.

    Pure data model - checking logic in RunCommandChecker._check_impl()

    Example YAML:
        success_criteria:
          # Simple exit-code check (original behavior)
          - type: "run_command"
            command: "python app.py"
            description: "Script must run successfully"

          # With stdout matching (replaces program_stdout_equals)
          - type: "run_command"
            command: "python hello.py"
            expected_stdout: "Hello, World!"
            stdout_match: "exact"
            description: "Script must output the correct text"

          # Continuous scoring from stdout (first line must be a float 0.0-1.0)
          - type: "run_command"
            command: "python score.py"
            score_from_stdout: true
            description: "Similarity score from scoring script"
    """

    type: Literal["run_command"] = "run_command"
    command: str = Field(description="Command to execute")
    timeout: int = Field(default=30, description="Timeout in seconds")
    expected_exit_code: int = Field(default=0, description="Expected exit code")
    expected_stdout: str | None = Field(
        default=None, description="Expected stdout content. When set, stdout is also checked."
    )
    stdout_match: Literal["exact", "contains", "regex"] = Field(
        default="exact",
        description="How to match stdout: 'exact' (stripped), 'contains' (substring), 'regex' (pattern)",
    )
    score_from_stdout: bool = Field(
        default=False,
        description=(
            "When true, read a float score (0.0-1.0) from the first line of stdout. "
            "Remaining lines are captured as details. Non-zero exit code or parse failure -> score 0.0. "
            "Mutually exclusive with expected_stdout."
        ),
    )

    @model_validator(mode="after")
    def check_score_from_stdout_exclusivity(self) -> RunCommandCriterion:
        if self.score_from_stdout and self.expected_stdout is not None:
            raise ValueError(
                "'score_from_stdout' and 'expected_stdout' are mutually exclusive. "
                + "Use 'score_from_stdout: true' for continuous float scoring, "
                + "or 'expected_stdout' for exact/contains/regex string matching."
            )
        return self


class FileMatchesRegexCriterion(BaseSuccessCriterion):
    """Check if file content matches a regex pattern.

    Pure data model - checking logic in SuccessChecker._check_file_matches_regex()
    """

    type: Literal["file_matches_regex"] = "file_matches_regex"
    path: str = Field(description="Path to the file to check")
    pattern: str = Field(description="Regex pattern that must match somewhere in the file")
    must_match: bool = Field(default=True, description="If True, pattern must match; if False, pattern must NOT match")
    flags: int = Field(default=0, description="Regex flags (e.g., re.IGNORECASE=2, re.MULTILINE=8, re.DOTALL=16)")


class RegexPattern(BaseModel):
    """A single regex pattern check within FileCheckCriterion."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(description="Regex pattern to match against file content")
    must_match: bool = Field(default=True, description="If True, pattern must match; if False, pattern must NOT match")
    flags: int = Field(default=0, description="Regex flags (e.g., re.IGNORECASE=2, re.MULTILINE=8, re.DOTALL=16)")


_OPERATORS_REQUIRING_EXPECTED = frozenset(
    {
        "equals",
        "not_equals",
        "contains",
        "gt",
        "gte",
        "lt",
        "lte",
        "type",
        "regex",
    }
)

# 8 admits common complete words while still catching truncated fragments;
# see reject_brittle_substring_contains docstring for full rationale.
_MIN_CONTAINS_LITERAL_LEN = 8


class JMESPathAssertion(BaseModel):
    """A single JMESPath assertion within JsonCheckCriterion."""

    model_config = ConfigDict(extra="forbid")

    expression: str = Field(description="JMESPath expression to evaluate against the parsed JSON")
    operator: Literal["equals", "not_equals", "contains", "gt", "gte", "lt", "lte", "type", "regex", "exists"] = Field(
        default="equals", description="Comparison operator (default: 'equals')"
    )
    expected: Any = Field(default=None, description="Expected value. Not required when operator is 'exists'.")

    @model_validator(mode="after")
    def validate_expected_for_operator(self) -> JMESPathAssertion:
        """Enforce that 'expected' is provided for operators that require it.

        Uses model_fields_set to distinguish 'expected' not provided from
        explicitly set to None (valid JSON null).
        """
        expected_provided = "expected" in self.model_fields_set
        if self.operator in _OPERATORS_REQUIRING_EXPECTED and not expected_provided:
            raise ValueError(f"'expected' is required when operator is '{self.operator}'")
        return self

    @model_validator(mode="after")
    def reject_brittle_substring_contains(self) -> JMESPathAssertion:
        """Reject `operator: contains` with a literal `expected` shorter than 8 stripped characters.

        A short `contains` substring against an agent-paraphrased JSON value
        flakes when the agent's wording shifts (e.g. `"scalat"` chosen as
        a fragment of "escalation" misses when the agent writes "review"
        instead). Whitespace-only literals (e.g. `"        "`) are equally
        brittle — they match almost any non-empty string — so length is
        measured after `.strip()`.

        Scope is deliberately narrow:
        - `regex` and `equals` are not validated: those operators are explicit
          author signals that brittle short matching is intentional (e.g. an
          anchored regex or a canonical machine name).
        - Non-string `expected` (numeric, list) is not validated: `contains`
          against a list is exact element equality, not substring matching.
        """
        if (
            self.operator == "contains"
            and isinstance(self.expected, str)
            and len(self.expected.strip()) < _MIN_CONTAINS_LITERAL_LEN
        ):
            raise ValueError(
                f"Brittle 'contains' assertion: expected literal {self.expected!r}"
                + f" has {len(self.expected.strip())} non-whitespace characters; short"
                + " substrings of paraphrased JSON values flake when agent wording varies."
                + " Use operator='regex' with an anchored pattern, or"
                + " operator='equals' with a canonical machine name."
            )
        return self


class JsonCheckCriterion(BaseSuccessCriterion):
    """Validate a JSON file: existence, parseability, schema conformance, and JMESPath assertions.

    Fractional scoring. File missing or invalid JSON -> 0.0.
    Only active categories (schema, assertions) contribute to the average.
    """

    type: Literal["json_check"] = "json_check"
    path: str = Field(description="Path to the JSON file (relative to sandbox root)")
    json_schema: str | None = Field(default=None, description="Path to JSON Schema file (relative to sandbox root)")
    assertions: list[JMESPathAssertion] = Field(
        default_factory=list, description="JMESPath assertions to evaluate against the parsed JSON"
    )


class FileCheckCriterion(BaseSuccessCriterion):
    """Unified file check: existence + string includes/excludes + regex patterns.

    Existence is implicit — if the file doesn't exist, all checks fail (score 0.0).
    All other fields are optional: if none are specified, this is a pure existence check.

    Scoring: fractional, computed as the average of active sub-check scores
    (includes score, excludes score, and patterns score). Only categories with
    non-empty lists contribute to the average, preventing score inflation.

    Pure data model - checking logic in FileCheckChecker._check_impl()

    Example YAML:
        success_criteria:
          - type: "file_check"
            path: "main.py"
            includes: ["from uipath import UiPath"]
            excludes: ["import os"]
            patterns:
              - pattern: "def main\\(.*\\):"
                must_match: true
            description: "main.py exists with correct imports and structure"
    """

    type: Literal["file_check"] = "file_check"
    path: str = Field(description="Path to the file to check (relative to sandbox root)")
    includes: list[str] = Field(default_factory=list, description="Strings that must be present in the file")
    excludes: list[str] = Field(default_factory=list, description="Strings that must NOT be present in the file")
    patterns: list[RegexPattern] = Field(
        default_factory=list, description="Regex patterns to check against file content"
    )


class ReferenceComparisonCriterion(BaseSuccessCriterion):
    """Compare agent code against reference solution.

    Uses the top-level `reference` block from TaskDefinition.
    The reference code is loaded by the orchestrator and passed to the success checker.

    Pure data model - checking logic in SuccessChecker._check_reference_comparison()

    Example YAML:
        success_criteria:
          - type: "reference_comparison"
            description: "Code structure matches reference"
            agent_file: "solution.py"
            comparison_method: "ast"
            similarity_threshold: 0.8
            weight: 1.0
            pass_threshold: 0.8
    """

    requires_agent: ClassVar[bool] = True

    type: Literal["reference_comparison"] = "reference_comparison"

    # Required fields
    agent_file: str = Field(description="Path to agent's generated file (relative to sandbox root)")

    comparison_method: Literal["ast", "token", "complexity"] = Field(
        default="ast",
        description="Method for comparing code: 'ast' (structure), 'token' (text), 'complexity' (metrics)",
    )

    similarity_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="Minimum similarity score to pass (0.0-1.0)"
    )

    @model_validator(mode="after")
    def align_threshold_to_similarity(self) -> ReferenceComparisonCriterion:
        """Align pass_threshold to similarity_threshold for clarity.

        Since similarity_threshold is the domain-specific field, it takes precedence.
        """
        self.pass_threshold = self.similarity_threshold
        return self


class CommandsEfficiencyCriterion(BaseSuccessCriterion):
    """Score agent tool-call efficiency relative to an expected budget.

    Score = expected / max(actual, expected). Yields 1.0 at or under budget.
    """

    requires_agent: ClassVar[bool] = True
    type: Literal["commands_efficiency"] = "commands_efficiency"
    expected_commands: int = Field(ge=1, description="Expected number of tool commands to complete the task")


class CommandExecutedCriterion(LiveSuccessCriterion):
    """Check whether the agent executed specific commands/tools.

    Inspects CommandTelemetry records from TurnRecord.commands to verify
    that the agent used specific tools or commands during evaluation.

    Pure data model - checking logic in CommandExecutedChecker._check_impl()

    Example YAML (positive — must run at least once):
        success_criteria:
          - type: "command_executed"
            description: "Agent used curl to fetch weather"
            tool_name: "Bash"
            command_pattern: "curl.*wttr\\.in"
            min_count: 1
            require_success: true

    Example YAML (negative — must NOT run; uses ``min_count: 0`` + ``max_count: 0``):
        success_criteria:
          - type: "command_executed"
            description: "Agent did NOT call the retired endpoint"
            tool_name: "Bash"
            command_pattern: "uip\\s+or\\s+users\\s+list"
            min_count: 0
            max_count: 0

    Example YAML (bounded range — between 3 and 5 calls):
        success_criteria:
          - type: "command_executed"
            description: "Agent retried the right number of times"
            tool_name: "Bash"
            command_pattern: "curl.*api\\.example\\.com"
            min_count: 3
            max_count: 5
    """

    requires_agent: ClassVar[bool] = True

    type: Literal["command_executed"] = "command_executed"
    tool_name: str | None = Field(default=None, description="Tool name filter (e.g., 'Bash'). None = any tool.")
    command_pattern: str | None = Field(
        default=None, description="Regex to match command parameters. None = any command."
    )
    min_count: int = Field(
        default=1,
        ge=0,
        description=(
            "Minimum matching commands required. ``0`` allows the criterion to "
            "pass when no commands match (combine with ``max_count: 0`` to "
            "express ``must NOT match``)."
        ),
    )
    max_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional maximum matching commands allowed (inclusive). ``None`` "
            "means no upper bound — the current default. When set, the criterion "
            "passes iff ``min_count <= match_count <= max_count``."
        ),
    )
    require_success: bool = Field(default=False, description="If True, only count successful commands.")
    exclude_pattern: str | None = Field(
        default=None,
        description=(
            "Regex that must NOT match. Commands matching both command_pattern and exclude_pattern are skipped."
        ),
    )

    @model_validator(mode="after")
    def _validate_count_bounds(self) -> CommandExecutedCriterion:
        """Reject impossible ranges where ``max_count < min_count``."""
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError(f"max_count ({self.max_count}) must be >= min_count ({self.min_count})")
        return self

    def live_decidable_polarities(self) -> frozenset[LivePolarity]:
        """Narrow to what THIS instance can decide mid-run.

        The checker's ``live_verdict`` (``criteria/command_executed.py``) can
        only:

        - ``pass`` when there is no upper bound and a positive floor
          (``max_count is None and min_count > 0``) — with an upper bound a
          pass is not final until end-of-run, so it never fires live; and
        - ``fail`` when there IS an upper bound (``max_count is not None``),
          the moment the count exceeds it (this includes the
          ``min_count: 0, max_count: 0`` "must-NOT-run" form).

        So these instance shapes leave a trigger inert (by design, so a
        dataset-fanned line works across row roles): ``on_pass: stop`` /
        ``decide_within`` with ``max_count`` set (pass can never fire); the
        implicit fail trigger with ``max_count: None`` (fail can never fire);
        ``min_count: 0, max_count: None`` (neither can ever fire).
        """
        decidable: set[LivePolarity] = set()
        if self.max_count is None:
            if self.min_count > 0:
                decidable.add("pass")
        else:
            decidable.add("fail")
        return frozenset(decidable)


class UiPathEvalCriterion(BaseSuccessCriterion):
    """Check evaluation results against UiPath agent performance.

    Pure data model - checking logic in UiPathEvalChecker._check_impl()

    Threshold semantics: all thresholds are *minimum acceptable values* — a metric
    passes if ``metric_value >= threshold``. For metrics where lower is better
    (e.g. latency), negate or invert the metric on the evaluation side rather than
    here (e.g. use ``throughput = 1 / avg_time`` and threshold on that instead).
    """

    type: Literal["uipath_eval"] = "uipath_eval"
    agent_name: str = Field(description="Name of the UiPath agent to evaluate")
    eval_set: str = Field(description="Evaluation set identifier")
    thresholds: dict[str, float] = Field(
        description=(
            "Minimum acceptable value per metric (e.g., {'accuracy': 0.8, 'f1': 0.75}). "
            "A metric passes if its value >= the threshold."
        )
    )


class ClassificationMatchCriterion(BaseSuccessCriterion):
    """Match a single label written by the agent to a file against ground truth.

    Reads the file, normalizes the content (strip + optional lowercase), and
    compares it to ``expected_label``. The observed label is the canonical form
    from ``allowed_labels`` when it matches; otherwise ``'(none)'`` when the
    file is missing / empty and ``'(other)'`` when the content is not in the
    allowed set. Both are recorded as sentinels so the suite rollup can show
    them as real failure classes in the confusion matrix.

    The checker returns a ``ClassificationCriterionResult`` (subclass of
    ``CriterionResult``) carrying observed + expected labels, which the
    suite aggregator reads to compute P/R/F1 per class and a confusion matrix.

    Example YAML:

        success_criteria:
          - type: "classification_match"
            path: "result.txt"
            expected_label: "positive"
            allowed_labels: [positive, negative]
            description: "Sentiment label matches ground truth"
    """

    type: Literal["classification_match"] = "classification_match"
    path: str = Field(description="Path to the file (relative to sandbox) containing the agent's predicted label")
    expected_label: str = Field(description="Ground-truth label for this row")
    allowed_labels: list[str] = Field(
        min_length=1,
        description="Canonical label set. File content not in this set is treated as '(other)'.",
    )
    case_sensitive: bool = Field(
        default=False,
        description="When False (default), matching is case-insensitive and labels are canonicalised.",
    )


class SkillTriggeredCriterion(LiveSuccessCriterion):
    """Binary classifier: did the agent engage the target skill during the run?

    Agent-agnostic. Observed label is ``"yes"`` when ``turn_records`` show the
    skill engaged under ``skill_name`` — Claude via an explicit ``Skill`` tool
    call, or any agent without that tool (e.g. Codex) by reading the skill's
    files off disk (a command parameter contains ``skills/<skill_name>/``).
    Otherwise ``"no"``. Expected label is ``"yes"`` iff ``expected_skill == skill_name``.

    Stack one criterion per skill against a single dataset labeled with
    ``expected_skill`` (the row's true skill, ``""`` for negatives) to get
    per-skill confusion matrices from the same agent traces.

    Returns a ``ClassificationCriterionResult`` so the suite-level
    aggregator produces accuracy / recall / F1 / confusion.

    Example YAML:

        success_criteria:
          - type: "skill_triggered"
            description: "uipath-maestro-flow activation"
            skill_name: uipath-maestro-flow
            expected_skill: "${row.expected_skill}"
            suite_thresholds: {recall.yes: 0.70}
    """

    requires_agent: ClassVar[bool] = True

    type: Literal["skill_triggered"] = "skill_triggered"
    expected_skill: str = Field(
        description="The row's expected skill (after substitution); empty string '' for negatives.",
    )
    skill_name: str = Field(
        description="Only count Skill invocations whose 'skill' parameter matches this name.",
    )

    def live_decidable_polarities(self) -> frozenset[LivePolarity]:
        """Per-instance narrowing under the checker's any-engagement latch.

        The checker's ``live_verdict`` (``criteria/skill_triggered.py``) can
        decide either polarity at the TYPE level, but a single INSTANCE only
        ever resolves one of them:

        - a **positive** criterion (``skill_name == expected_skill``) can only
          live-``pass`` (the expected skill engaging is a decidable hit; its
          absence is not knowable mid-run);
        - a **distractor/negative** criterion (``skill_name != expected_skill``,
          including the ``expected_skill == ""`` negatives) can only
          live-``fail`` (a wrong skill engaging is a decidable miss; its
          absence is not).

        ``EarlyStopWatcher`` consults this set to decide which triggers are
        live per instance: on a positive row ``on_pass: stop`` and
        ``decide_within`` are live while the implicit fail trigger is inert;
        on a distractor row the reverse. That per-row adaptivity is what lets
        one dataset-fanned YAML line (same block on every row) serve both
        roles.
        """
        expected_yes = self.expected_skill == self.skill_name
        return frozenset({"pass"}) if expected_yes else frozenset({"fail"})


class LLMJudgeCriterion(BaseSuccessCriterion):
    """Have an LLM grade the task's final state against an author-supplied prompt.

    The judge can be given any combination of: sandbox files, the agent's last-turn
    output, a tool-call summary, and the reference solution. It reports its verdict
    by emitting a single ``submit_verdict`` tool call with ``score`` (float),
    ``rationale`` (string), and ``findings`` (list of strings). Tool-call shape is
    forced via ``tool_choice`` on every backend; the model never returns prose.

    Continuous scoring. LLM error or a "did not call submit_verdict" diagnostic
    -> 0.0 with error. Score is clamped to [0.0, 1.0] by the ``JudgeVerdict``
    validator. Non-numeric / non-finite score -> 0.0 with error.
    """

    # Strict YAML-key validation: catch typos at load time rather than silently
    # ignoring an unknown key (e.g. ``capture_transcripts:`` instead of
    # ``capture_transcript:``) and producing a misconfigured judge.
    model_config = ConfigDict(extra="forbid")

    type: Literal["llm_judge"] = "llm_judge"

    # Override the BaseSuccessCriterion default of 0.9 — that's calibrated for binary
    # checks like file_exists where partial = fail. Judges produce continuous scores
    # that rarely emit 1.0 even for excellent solutions; 0.7 matches the "good enough
    # for a strict reviewer" semantics most authors want. Override per task as needed.
    pass_threshold: float = Field(default=0.7, ge=0.0, le=1.0, description="Minimum score to pass (default 0.7).")

    enabled: bool = Field(
        default=True,
        description=(
            "Master toggle for this criterion. When False the judge is NOT called — the criterion "
            "returns a skipped result (score=1.0, details='(skipped: enabled=false)') with no LLM "
            "cost. Useful for A/B comparisons across experiment variants where you want to keep the "
            "criterion in the YAML but not run it under a specific variant."
        ),
    )
    prompt: str = Field(
        description=(
            "Grading instructions shown to the judge. Describe what 'good' looks like "
            "and how observations map to a 0.0-1.0 score."
        )
    )
    files: list[str] = Field(
        default_factory=list,
        description=(
            "Paths whose contents are shown to the judge. Plain entries are sandbox-relative; "
            "entries prefixed with '$TASK_DIR/' are read from the host filesystem relative to "
            "the task YAML's parent directory (useful for shared rubrics outside the sandbox). "
            "Missing files are rendered as '<file not found>' so the rubric can penalize them."
        ),
    )
    include_reference: bool = Field(
        default=True,
        description=(
            "When true (default) and task.reference is set, include the reference solution in "
            "the judge prompt. Silently omitted if no reference is configured. Never shown to "
            "the agent. Set to false if you want the reference to drive a non-judge consumer "
            "(e.g. ``reference_comparison``) without showing it to the LLM grader."
        ),
    )
    include_agent_output: bool = Field(
        default=False,
        description=(
            "When true, include the latest agent turn's raw output in the judge prompt. "
            "Wrapped as UNTRUSTED DATA. No-op when turn_records is unavailable. Default false "
            "because the agent's narration is usually redundant with the files it produced "
            "(declared via ``files`` or visible to the agent_judge via tool access)."
        ),
    )
    include_tool_calls: bool = Field(
        default=False,
        description=(
            "When true, include a summary of the latest agent turn's tool calls "
            "(via summarize_commands). No-op when turn_records is unavailable."
        ),
    )
    include_dialog: bool = Field(
        default=False,
        description=(
            "When true, include the full user<->agent conversation across all turns "
            "in the judge prompt. In simulation mode the user side is generated by an "
            "LLM simulator and may invent premises — the judge should treat any claim "
            "made only by the simulated user as possibly fabricated, and not penalize "
            "the agent for going along with it unless the task description contradicts it."
        ),
    )
    max_dialog_chars: int = Field(
        default=80_000,
        gt=0,
        description=(
            "Aggregate cap on dialog text rendered into the judge prompt. Prevents an "
            "N-turn simulation from blowing out the judge's context window. Per-message "
            "truncation uses max_file_chars; trailing turns are dropped when this "
            "aggregate budget is exceeded (a degraded note is recorded)."
        ),
    )
    model: str = Field(
        default=DEFAULT_JUDGE_MODEL,
        description=(
            "Judge model id (e.g. 'anthropic.claude-sonnet-4-6'). "
            "On a BedrockRoute / DirectRoute the value is auto-translated: "
            "trailing '-vN[:M]' suffixes and the 'anthropic.' prefix are stripped where "
            "the backend doesn't accept them; on Bedrock the cross-region inference-profile "
            "prefix is added based on AWS_REGION."
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(
        default=2000,
        gt=0,
        description=(
            "Output token cap. Defaults to 2000 — large enough for the verbose verdict "
            "(score + rationale + a handful of findings) without runaway."
        ),
    )
    max_file_chars: int = Field(
        default=20_000,
        gt=0,
        description="Per-file content truncation applied before building the prompt.",
    )
    capture_transcript: bool = Field(
        default=True,
        description=(
            "When true, persist a ``JudgeTranscript`` (raw verdict + rendered prompts + "
            "token usage) to a sibling ``judge-<idx>.yaml`` file next to ``task.json``. "
            "Set to false to drop the transcript when on-disk size matters (e.g. 1000-row "
            "datasets). The verbose ``findings`` field on the result is persisted regardless — "
            "only the per-call transcript file is gated by this flag."
        ),
    )
    max_transcript_chars: int = Field(
        default=100_000,
        gt=0,
        description=(
            "Aggregate cap on captured transcript text (raw_verdict + judge_prompt + "
            "judge_system_prompt, plus tool-call detail / result preview lines for "
            "agent_judge). Budget is split 60% verdict / 30% prompt / 10% system. "
            "Truncation marks the transcript as ``truncated=True``."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_verdict_channel(cls, data: Any) -> Any:
        return _reject_removed_verdict_channel(data)


class AgentJudgeCriterion(BaseSuccessCriterion):
    """Spawn a Claude Code SDK agent as the judge.

    The judge runs in an isolated copy of the sandbox with tool access (Bash, Read,
    Glob, Grep by default) and reports its verdict by calling the in-process
    ``submit_verdict`` MCP tool (``mcp__coder_eval_judge__submit_verdict``,
    force-added to ``allowed_tools``) exactly once with ``score`` / ``rationale``
    / ``findings``. The SDK has no ``tool_choice`` equivalent so the channel
    relies on system-prompt discipline; a judge that never calls the tool
    scores 0.0 with a "did not call submit_verdict" diagnostic.

    File access: the judge has live access to the sandbox copy as its working
    directory and can load files via its tools (``Read``, ``Glob``). The optional
    ``files`` field pre-attaches selected file contents to the prompt envelope —
    useful when you want a fast verdict (single turn, no tool calls) or want to
    grade with the narrowest tool surface (``allowed_tools=[]``). Pre-attach and
    tool-driven inspection compose: the judge sees the pre-attached blocks and
    can still ``Read`` anything else.

    SECURITY: The judge runs with the evaluator's API credentials and can execute
    arbitrary Bash by default. Four attack surfaces:
      1. Malicious generation artifacts the judge executes (e.g. via `python x.py`).
      2. Prompt injection from included agent_output / tool-call summaries.
      3. Credential exfiltration via any network-capable tool (primarily Bash).
      4. Hooks / MCP servers planted by the main agent (e.g. `.claude/settings.json`
         or `.mcp.json` dropped into the sandbox) that would run before any LLM turn
         and before allowed_tools gating. The judge sets ``setting_sources=[]`` and
         excludes both paths from the sandbox copy, so neither gets loaded.
    Use ``llm_judge`` for scenarios with adversarial generation. Narrow
    ``allowed_tools`` per task when Bash is not needed.

    Continuous scoring. Verdict-validation errors or a "did not call" diagnostic
    -> 0.0. Score clamped to [0.0, 1.0] by the ``JudgeVerdict`` validator.
    """

    # Strict YAML-key validation: catch typos at load time rather than silently
    # ignoring an unknown key and producing a misconfigured judge.
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_judge"] = "agent_judge"

    # Override the BaseSuccessCriterion default of 0.9 — that's calibrated for binary
    # checks. Judges produce continuous scores that rarely emit 1.0; 0.7 matches the
    # "good enough for a strict reviewer" semantics most authors want. Override per task.
    pass_threshold: float = Field(default=0.7, ge=0.0, le=1.0, description="Minimum score to pass (default 0.7).")

    enabled: bool = Field(
        default=True,
        description=(
            "Master toggle for this criterion. When False the judge is NOT spawned — the criterion "
            "returns a skipped result (score=1.0, details='(skipped: enabled=false)') with no LLM "
            "cost. Useful for A/B comparisons across experiment variants where you want to keep the "
            "criterion in the YAML but not run it under a specific variant."
        ),
    )
    # Prompt & context — mirrors LLMJudgeCriterion for author consistency
    prompt: str = Field(description="Evaluation instructions for the judge agent")
    files: list[str] = Field(
        default_factory=list,
        description=(
            "Paths whose contents are pre-attached to the judge prompt. Plain entries are "
            "sandbox-relative; entries prefixed with '$TASK_DIR/' are read from the host "
            "filesystem relative to the task YAML's parent directory (useful for shared rubrics "
            "outside the sandbox). Missing files are rendered as '<file not found>' so the "
            "rubric can penalize them. Empty by default — without entries, the judge inspects "
            "the sandbox copy via its tools instead."
        ),
    )
    include_reference: bool = Field(
        default=True,
        description=(
            "When true (default) and task.reference is set, mount the reference for the judge. "
            "For ``code`` / ``file`` references, the content is inlined into the prompt. For "
            "``directory`` references, the tree is copied into ``_reference/`` in the judge's "
            "working dir for Read/Glob browsing. Silently omitted if no reference is configured. "
            "Set to false if a reference is configured for ``reference_comparison`` only and "
            "should NOT be visible to the LLM grader."
        ),
    )
    include_agent_output: bool = Field(
        default=False,
        description=(
            "Include the latest agent turn's raw output in the judge prompt (UNTRUSTED). "
            "Default false because agent_judge has live tool access to the sandbox copy and "
            "can ``Read`` the agent's files directly — inlining narration is usually redundant."
        ),
    )
    include_tool_calls: bool = Field(
        default=False,
        description="Include summarized tool-call telemetry from the latest agent turn.",
    )
    include_dialog: bool = Field(
        default=False,
        description=(
            "Include the full user<->agent conversation across all turns. In simulation "
            "mode the user side is generated by an LLM simulator and may invent premises — "
            "the judge should treat any claim made only by the simulated user as possibly "
            "fabricated, and not penalize the agent for going along with it unless the task "
            "description contradicts it."
        ),
    )
    max_dialog_chars: int = Field(
        default=80_000,
        gt=0,
        description=(
            "Aggregate cap on dialog text rendered into the judge prompt. Prevents an "
            "N-turn simulation from blowing out the judge's context window. Per-message "
            "truncation uses max_file_chars; trailing turns are dropped when this "
            "aggregate budget is exceeded (a degraded note is recorded)."
        ),
    )
    max_file_chars: int = Field(
        default=20_000,
        gt=0,
        description=(
            "Per-message truncation budget for trajectory blocks (agent_output, dialog turns). "
            "agent_judge no longer pre-attaches files — the judge reads them via its tools — so "
            "this only applies to trajectory injection."
        ),
    )

    # Judge agent config
    max_turns: int = Field(
        default=50,
        gt=0,
        description=(
            "Inner-loop turn limit for the judge agent. Generous default — typical judge runs "
            "use 5-15 turns; the cap mostly matters when grading complex multi-file solutions "
            "where the judge needs to read across many files. ``turn_timeout`` (default 300s) "
            "bounds wall-clock per turn, so the practical cost cap is tokens, not time."
        ),
    )
    turn_timeout: int = Field(
        default=300,
        ge=10,
        description="Wall-clock timeout for the judge turn (seconds). Minimum 10.",
    )
    # Intentionally the closed built-in AgentConfig union, NOT ResolvedAgentConfig:
    # agent_judge spawns a Claude Code SDK sub-agent (SubAgentRunner) with evaluator
    # credentials, so a plugin agent kind is deliberately not accepted as a judge.
    agent: AgentConfig = Field(
        default_factory=_default_judge_agent_config,
        description=(
            "Judge agent configuration (built-in kinds only — agent_judge runs a Claude Code "
            "sub-agent). Defaults to a sonnet, bypass-permissions, read-only toolkit suitable "
            "for investigation-style judging. Override fields per task as needed. For security, "
            "the judge always runs with setting_sources=[] regardless of what this field "
            "declares — see _build_agent_config."
        ),
    )
    capture_transcript: bool = Field(
        default=True,
        description=(
            "When true, persist a ``JudgeTranscript`` (tool calls + token usage + raw verdict + "
            "rendered prompts) to a sibling ``judge-<idx>.yaml`` file next to ``task.json``. "
            "Set to false to drop the transcript when on-disk size matters (e.g. 1000-row "
            "datasets). The verbose ``findings`` field on the result is persisted regardless — "
            "only the trajectory log is gated by this flag."
        ),
    )
    max_transcript_chars: int = Field(
        default=100_000,
        gt=0,
        description=(
            "Aggregate cap on captured transcript text (raw_verdict + judge_prompt + "
            "judge_system_prompt + tool detail / result_preview lines). Budget is split "
            "60% verdict / 30% prompt / 10% system, with tool calls taking priority. "
            "Truncation marks the transcript as ``truncated=True``."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_verdict_channel(cls, data: Any) -> Any:
        return _reject_removed_verdict_channel(data)


# Discriminated union of all success criteria. The `type` tag is REQUIRED in
# dict/YAML input: a missing or unknown tag raises one crisp discriminator error
# instead of smart-union coercion across every variant. The per-variant
# `type: Literal[...] = "<tag>"` defaults remain, so direct construction
# (e.g. FileExistsCriterion(path=...)) and model_dump() serialization are unaffected.
SuccessCriterion = Annotated[
    FileExistsCriterion
    | FileContainsCriterion
    | RunCommandCriterion
    | FileMatchesRegexCriterion
    | FileCheckCriterion
    | JsonCheckCriterion
    | ReferenceComparisonCriterion
    | CommandExecutedCriterion
    | CommandsEfficiencyCriterion
    | UiPathEvalCriterion
    | ClassificationMatchCriterion
    | SkillTriggeredCriterion
    | LLMJudgeCriterion
    | AgentJudgeCriterion,
    Field(discriminator="type"),
]
