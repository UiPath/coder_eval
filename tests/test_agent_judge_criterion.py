"""Checker-level integration tests for agent_judge.

Sandbox-copy + subprocess-lifecycle coverage lives in test_sub_agent_runner.py.
Verdict-tool extractor unit tests live in test_verdict_tool.py.
This file covers the orchestration glue: prompt assembly, result mapping,
backend routing, and config propagation.

Test pattern: tests mock ``ClaudeCodeAgent`` via ``_AGENT_PATCH_PATH`` and
pass a JSON ``agent_output`` for legacy convenience; an autouse fixture
wraps ``SubAgentRunner.run`` so that JSON-shaped agent output is parsed
into the runner's ``VerdictCapture``, simulating what the real
``submit_verdict`` tool call would do.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from coder_eval.criteria import init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.evaluation.sub_agent import SubAgentRunner
from coder_eval.models import (
    AgentJudgeCriterion,
    JudgeVerdict,
    TurnRecord,
)
from coder_eval.models.routing import DirectRoute, ProxyRoute
from coder_eval.sandbox import Sandbox


# ClaudeCodeAgent is now imported inside SubAgentRunner; tests patch the runner's binding.
_AGENT_PATCH_PATH = "coder_eval.evaluation.sub_agent.ClaudeCodeAgent"


def _make_turn(agent_output: str, duration: float = 1.5) -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="(judge prompt)",
        agent_output=agent_output,
        duration_seconds=duration,
    )


def _make_mock_agent(agent_output: str) -> MagicMock:
    agent = MagicMock()
    agent.start = AsyncMock(return_value=None)
    agent.communicate = AsyncMock(return_value=_make_turn(agent_output))
    agent.stop = AsyncMock(return_value=None)
    agent.kill = AsyncMock(return_value=None)
    return agent


# Original ``SubAgentRunner.run`` — wrapped below so JSON-shaped agent_output
# in tests populates the runner's ``VerdictCapture`` exactly as the real
# ``submit_verdict`` tool call would.
_orig_run = SubAgentRunner.run


def _run_with_capture_simulation(self, user_msg, *, max_turns, turn_timeout):
    turn = _orig_run(self, user_msg, max_turns=max_turns, turn_timeout=turn_timeout)
    if self.capture is not None and self.capture.verdict is None and self.capture.error is None:
        try:
            data = _json.loads(turn.agent_output)
        except (ValueError, TypeError):
            return turn
        try:
            self.capture.verdict = JudgeVerdict.model_validate(data)
        except ValidationError as e:
            # Surface the same legacy-vocabulary diagnostic ``submit_verdict``'s
            # ``@tool`` handler would have written. Bypasses a circular test-only
            # import by re-deriving the first error message inline.
            from coder_eval.evaluation.verdict_tool import _format_validation_error

            self.capture.error = _format_validation_error(e)
    return turn


@pytest.fixture(autouse=True)
def _simulate_capture_population(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge legacy test mocks (JSON in ``turn.agent_output``) to the typed verdict channel.

    Real production runs populate ``capture`` via the ``submit_verdict`` SDK tool
    handler; tests stub the agent so the tool never fires. This fixture parses
    the agent's output as JSON and populates the runner's capture so the
    criterion sees the verdict via the standard ``extract_verdict_from_capture``
    path.
    """
    monkeypatch.setattr(SubAgentRunner, "run", _run_with_capture_simulation)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="agent_judge_test")
    sb.sandbox_dir = tmp_path
    return sb


@pytest.fixture(autouse=True)
def _ensure_registry_initialized() -> None:
    init_criteria(validate=False)


@pytest.fixture
def direct_route() -> DirectRoute:
    """Default route for unit tests — equivalent to ``api_backend=DIRECT``."""
    return DirectRoute()


# --- happy path + scoring end-to-end ---


def test_agent_judge_happy_path(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="grade the project", prompt="Is this code valid?")
    mock_agent = _make_mock_agent('{"score": 0.85, "rationale": "looks good"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.85
    assert result.error is None
    assert "looks good" in (result.details or "")
    assert "duration:" in (result.details or "")


def test_agent_judge_score_clamped_high(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 1.7, "rationale": "too high"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 1.0


def test_agent_judge_score_clamped_low(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": -0.4, "rationale": "negative"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0


@pytest.mark.parametrize("raw_score", ["NaN", "Infinity", "-Infinity"])
def test_agent_judge_rejects_non_finite(sandbox: Sandbox, direct_route: DirectRoute, raw_score: str) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent(f'{{"score": {raw_score}, "rationale": "oops"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "finite" in result.error


def test_agent_judge_missing_score_key(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"rationale": "forgot score"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "missing" in result.error


def test_agent_judge_no_verdict_surfaces_untrusted_output(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """A judge that finishes without calling submit_verdict surfaces with UNTRUSTED_JUDGE_OUTPUT in details."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent("I ran xmllint and everything looks great")
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"
    assert (result.details or "").startswith("UNTRUSTED_JUDGE_OUTPUT:")


# --- timeout mapping ---


def test_agent_judge_turn_timeout_maps_to_zero(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    from coder_eval.errors.timeout import TurnTimeoutError

    criterion = AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=30)
    mock_agent = _make_mock_agent("irrelevant")
    mock_agent.communicate.side_effect = TurnTimeoutError(30.0, task_id="t", iteration=1)

    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.0
    assert result.error is not None
    assert "TurnTimeoutError" in result.error
    assert "timed out" in (result.details or "")


# --- config flow-through ---


def test_agent_judge_config_propagates(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    from coder_eval.models import AgentConfig

    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        max_turns=3,
        turn_timeout=45,
        agent=AgentConfig(
            type="claude-code",
            model="claude-sonnet-4-6",
            permission_mode="plan",
            allowed_tools=["Read", "Grep"],
            disallowed_tools=["Bash"],
            sdk_options={"effort": "low"},
        ),
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    (agent_config,) = mock_cls.call_args.args
    assert agent_config.model == "claude-sonnet-4-6"
    assert agent_config.permission_mode == "plan"
    assert set(agent_config.allowed_tools or []) == {"Read", "Grep", "mcp__coder_eval_judge__submit_verdict"}
    assert agent_config.disallowed_tools == ["Bash"]
    assert agent_config.setting_sources == []
    assert agent_config.sdk_options == {"effort": "low"}
    # max_turns and turn_timeout are passed as call-time args to communicate(),
    # not stored on AgentConfig.
    mock_agent.communicate.assert_awaited_once()
    kwargs = mock_agent.communicate.call_args.kwargs
    assert kwargs["timeout"] == 45.0
    assert kwargs["max_turns"] == 3


def test_agent_judge_rejects_turn_timeout_below_ten() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="turn_timeout"):
        AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=5)


def test_agent_judge_default_ignores_include_claude_and_mcp() -> None:
    c = AgentJudgeCriterion(description="x", prompt="grade")
    assert ".claude" in c.agent.ignore_patterns
    assert ".mcp.json" in c.agent.ignore_patterns


def test_agent_judge_old_shape_rejected() -> None:
    """Old-shape YAML (criterion-level model / permission_mode / ...) raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentJudgeCriterion(description="x", prompt="grade", permission_mode="plan")


def test_agent_judge_default_agent_config_is_per_instance() -> None:
    """Each AgentJudgeCriterion gets a fresh AgentConfig — no shared mutable refs."""
    inst1 = AgentJudgeCriterion(description="x", prompt="grade")
    inst2 = AgentJudgeCriterion(description="x", prompt="grade")
    assert inst1.agent is not inst2.agent
    assert inst1.agent.allowed_tools is not inst2.agent.allowed_tools

    inst1.agent.allowed_tools.append("Write")
    assert "Write" not in (inst2.agent.allowed_tools or [])


def test_agent_judge_build_agent_config_does_not_mutate_criterion(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Calling _build_agent_config (via SuccessChecker) leaves criterion.agent untouched.

    Defensive: model_copy(deep=True) ensures system_prompt rebuild for each row
    doesn't leak back into the YAML-loaded criterion across dataset fan-out.
    """
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    original_setting_sources = criterion.agent.setting_sources
    original_system_prompt = criterion.agent.system_prompt
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert criterion.agent.setting_sources == original_setting_sources
    assert criterion.agent.system_prompt == original_system_prompt


def test_agent_judge_setting_sources_forced_empty(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """_build_agent_config always sets setting_sources=[] regardless of YAML."""
    from coder_eval.models import AgentConfig

    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        agent=AgentConfig(type="claude-code", setting_sources=["project"]),
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    (agent_config,) = mock_cls.call_args.args
    assert agent_config.setting_sources == []


def test_agent_judge_partial_agent_block_preserves_judge_defaults(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """A partial agent: block must not clobber the judge's hardened defaults.

    Before the merge fix in _build_agent_config, supplying only ``model:`` in
    YAML caused Pydantic to construct a fresh AgentConfig with ``permission_mode``
    falling back to acceptEdits, ``allowed_tools=None`` (==> all tools), and
    ``ignore_patterns=[]`` — silently dropping the security floor.
    """
    from coder_eval.models import AgentConfig

    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        agent=AgentConfig(type="claude-code", model="claude-haiku-4-5-20251001"),
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    (agent_config,) = mock_cls.call_args.args
    assert agent_config.permission_mode == "bypassPermissions"
    assert set(agent_config.allowed_tools or []) == {
        "Bash",
        "Read",
        "Glob",
        "Grep",
        "mcp__coder_eval_judge__submit_verdict",
    }
    assert ".claude" in agent_config.ignore_patterns
    assert ".mcp.json" in agent_config.ignore_patterns
    assert "_reference" in agent_config.ignore_patterns


def test_agent_judge_sdk_options_deep_merge_with_growing_defaults(
    sandbox: Sandbox, direct_route: DirectRoute, monkeypatch
) -> None:
    """Future-proof: when judge defaults grow non-empty sdk_options and the user
    supplies a partial sdk_options, the deep-merge in _build_agent_config must
    preserve default keys not overridden by the user.

    Today ``_default_judge_agent_config().sdk_options`` is empty, so the merge is
    a no-op. This test simulates a future where defaults add e.g. ``effort:
    medium``, and locks in that a user supplying ``{include_partial_messages:
    True}`` does NOT wipe the default's ``effort`` key.
    """
    from coder_eval.models import AgentConfig
    from coder_eval.models.criteria import _default_judge_agent_config

    real_default = _default_judge_agent_config

    def _default_with_sdk_options() -> AgentConfig:
        cfg = real_default()
        cfg.sdk_options = {"effort": "medium", "fallback_model": "claude-haiku-4-5-20251001"}
        return cfg

    # Patch both call sites (the model factory and the checker's import).
    monkeypatch.setattr("coder_eval.models.criteria._default_judge_agent_config", _default_with_sdk_options)
    monkeypatch.setattr("coder_eval.criteria.agent_judge._default_judge_agent_config", _default_with_sdk_options)

    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        agent=AgentConfig(type="claude-code", sdk_options={"include_partial_messages": True, "effort": "high"}),
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    (agent_config,) = mock_cls.call_args.args
    # User-set keys win; default keys not overridden survive.
    assert agent_config.sdk_options == {
        "include_partial_messages": True,
        "effort": "high",
        "fallback_model": "claude-haiku-4-5-20251001",
    }


def test_agent_judge_security_ignore_patterns_floor_enforced(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """User-supplied ignore_patterns are merged with the security floor, never replace it."""
    from coder_eval.models import AgentConfig

    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        agent=AgentConfig(type="claude-code", ignore_patterns=["custom_dir"]),
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    (agent_config,) = mock_cls.call_args.args
    assert "custom_dir" in agent_config.ignore_patterns
    assert ".claude" in agent_config.ignore_patterns
    assert ".mcp.json" in agent_config.ignore_patterns
    assert "_reference" in agent_config.ignore_patterns


# --- PROXY routing ---


def test_agent_judge_proxy_backend_routes_through_proxy(sandbox: Sandbox) -> None:
    """When SuccessChecker carries a ProxyRoute, the judge's sub-agent is
    constructed with that exact route — pointing at the orchestrator's
    running LLMGW proxy port."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.9, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False, route=ProxyRoute(port=12345)).check(criterion)

    assert result.error is None
    assert result.score == 0.9
    assert mock_cls.call_args.kwargs["route"] == ProxyRoute(port=12345)


def test_agent_judge_with_no_route_raises(sandbox: Sandbox) -> None:
    """agent_judge requires a route; without one the precondition assert surfaces
    as an error result (caught by @handle_criterion_errors)."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    with patch(_AGENT_PATCH_PATH) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 0.0
    assert result.error is not None
    assert "agent_judge requires a route" in result.error
    mock_cls.assert_not_called()


def test_success_checker_threads_route_to_check(sandbox: Sandbox) -> None:
    """The route passed to SuccessChecker reaches arbitrary checkers as the
    ``route`` kwarg of ``BaseCriterion.check`` — not just agent_judge."""
    from coder_eval.criteria.file_exists import FileExistsChecker
    from coder_eval.models import FileExistsCriterion

    route = ProxyRoute(port=999)
    checker = SuccessChecker(sandbox, init_registry=False, route=route)
    criterion = FileExistsCriterion(description="x", path="anywhere")

    with patch.object(FileExistsChecker, "_check_impl", wraps=FileExistsChecker()._check_impl) as spy:
        checker.check(criterion)

    assert spy.call_args.kwargs["route"] == route


# --- prompt / context assembly ---


def test_agent_judge_files_empty_default_uses_tool_only_prompt(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """When ``files`` is left at its empty default, the prompt points the judge at
    its working directory and renders no FILE blocks — the judge inspects via tools."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    assert criterion.files == []

    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "partial"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    user_msg = mock_agent.communicate.call_args.args[0]
    assert "--- FILE:" not in user_msg
    assert "Use Read/Glob/Grep to investigate" in user_msg


def test_agent_judge_pre_attaches_files_when_set(sandbox: Sandbox, direct_route: DirectRoute, tmp_path: Path) -> None:
    """When ``files=[...]`` is set, the named files' contents are inlined as FILE blocks.

    Pre-attachment is the fast-verdict path: with ``allowed_tools=[]`` the judge has
    no tool surface and decides from the inlined blocks alone — used by the smoke
    suite (``tasks/smoke_agent_judge.yaml``) and any task wanting llm_judge-style
    grading with the agent_judge sub-agent lifecycle.
    """
    sandbox_path = sandbox.sandbox_dir
    assert sandbox_path is not None
    (sandbox_path / "greet.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n")

    criterion = AgentJudgeCriterion(description="x", prompt="grade", files=["greet.py"])
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "matches rubric"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    user_msg = mock_agent.communicate.call_args.args[0]
    assert "--- FILE: greet.py ---" in user_msg
    assert "def greet(name: str) -> str:" in user_msg
    # Working-directory guidance is replaced by the pre-attach framing.
    assert "Use Read/Glob/Grep to investigate" not in user_msg
    assert "pre-attached" in user_msg


def test_agent_judge_parse_error_scrubs_reference_from_error_field(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Parse errors can echo the raw score value — must be scrubbed before persisting.

    Regression for a review finding: if the judge returns a malformed verdict that
    stuffs the reference sentinel into the score field, the parser surfaces the raw
    value in its diagnostic. That diagnostic lands in `error` and must be scrubbed.
    """
    sentinel = "REFERENCE_LEAK_VIA_ERROR_777"
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent(f'{{"score": "{sentinel}", "rationale": "ok"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    for field_value in (result.details, result.error):
        assert field_value is None or sentinel not in field_value


def test_agent_judge_parse_error_details_scrubs_before_truncating(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """SECURITY regression: scrubbing must run BEFORE the 500-char truncation in `details`.

    ``scrub_reference`` uses ``str.replace`` and only matches the secret as a contiguous
    whole string. If we sliced to 500 chars first and then scrubbed, a reference longer
    than the slice would survive as an unmatched prefix in the persisted details field.
    Trigger: an agent_judge run with ``include_reference=True`` whose output begins with
    a partial echo of the reference and is then malformed — the slice + scrub-after-slice
    order would land a recognizable chunk of the reference into ``details``.
    """
    # 800-char sentinel — longer than the 500-char details slice so a slice-before-scrub
    # path would leave the leading 500-char prefix in the persisted field.
    sentinel = "SCRUB_BEFORE_TRUNCATE_SENTINEL_" + "Z" * 800
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    # Non-JSON output starting with the full reference content guarantees the parse error
    # path is hit AND that the reference sits in the first 500 chars of `agent_output`.
    mock_agent = _make_mock_agent(f"{sentinel} — informal review, no JSON here")
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    # No portion of the reference body (e.g. any 100-char run of 'Z') should survive
    # in the persisted details — scrub-before-slice replaces the full sentinel with
    # the short ``<reference redacted>`` marker before any truncation.
    leak_payload = "Z" * 100
    assert leak_payload not in (result.details or "")
    assert "SCRUB_BEFORE_TRUNCATE_SENTINEL" not in (result.details or "")


def test_agent_judge_include_reference_scrubbed_from_details(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Reference is shown to the judge but scrubbed from persisted details.

    Policy matches ``llm_judge`` — a misbehaving judge that echoes the reference in
    its rationale should not leak it into on-disk ``CriterionResult.details``.
    """
    sentinel = "SENTINEL_REFERENCE_999"
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_reference=True,
    )
    mock_agent = _make_mock_agent(f'{{"score": 0.7, "rationale": "matches {sentinel}"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    user_msg = mock_agent.communicate.call_args.args[0]
    assert sentinel in user_msg
    assert sentinel not in (result.details or "")


def test_agent_judge_include_reference_false_omits_reference(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    sentinel = "SHOULD_NOT_APPEAR_42"
    # include_reference defaults to True now; opt out explicitly when grading material
    # is configured for non-judge consumers only.
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=False)
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, reference_code=sentinel)
    user_msg = mock_agent.communicate.call_args.args[0]
    assert sentinel not in user_msg


def test_agent_judge_include_agent_output_and_tool_calls(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    from datetime import datetime

    from coder_eval.models import CommandTelemetry

    command = CommandTelemetry(
        tool_name="Bash",
        tool_id="t1",
        timestamp=datetime.now(),
        parameters={"command": "uip rpa get-errors"},
        result_status="success",
        sequence_number=0,
    )
    turn = TurnRecord(
        iteration=1,
        user_input="create xaml",
        agent_output="Created the file successfully",
        commands=[command],
    )
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=[turn])

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "AGENT OUTPUT (UNTRUSTED" in user_msg
    assert "Created the file successfully" in user_msg
    assert "AGENT TOOL CALLS (UNTRUSTED" in user_msg
    assert "uip rpa get-errors" in user_msg


def test_agent_judge_include_dialog_renders_all_turns(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    turns = [
        TurnRecord(iteration=1, user_input="add a button", agent_output="added"),
        TurnRecord(iteration=2, user_input="make it red", agent_output="done"),
    ]
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=turns)

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" in user_msg
    assert "simulated user" in user_msg
    assert "[Turn 1] USER:\nadd a button" in user_msg
    assert "[Turn 1] AGENT:\nadded" in user_msg
    assert "[Turn 2] USER:\nmake it red" in user_msg
    assert "[Turn 2] AGENT:\ndone" in user_msg


def test_agent_judge_include_dialog_no_turns_records_degraded_note(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" not in user_msg
    assert "include_dialog requested but no turn records available" in (result.details or "")


def test_agent_judge_include_dialog_omitted_when_false(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    turn = TurnRecord(iteration=1, user_input="add a button", agent_output="added")
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=[turn])

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" not in user_msg
    assert "add a button" not in user_msg


def test_agent_judge_surfaces_degraded_notes_when_turn_records_missing(
    sandbox: Sandbox, direct_route: DirectRoute
) -> None:
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    mock_agent = _make_mock_agent('{"score": 0.9, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.9
    details = result.details or ""
    assert "include_agent_output requested but no turn records available" in details
    assert "include_tool_calls requested but no turn records available" in details
    assert "duration:" in details


# --- verbose verdict + transcript persistence ---


def _make_turn_with_commands(
    agent_output: str,
    commands: list,
    duration: float = 2.5,
) -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="(judge prompt)",
        agent_output=agent_output,
        commands=commands,
        duration_seconds=duration,
    )


def test_agent_judge_persists_findings(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    verdict = (
        '{"score": 0.7, "rationale": "ok", '
        '"findings": ["main.xaml is well-formed — correct", '
        '"missing test for happy path — minor deviation"]}'
    )
    mock_agent = _make_mock_agent(verdict)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.7
    assert getattr(result, "findings", []) == [
        "main.xaml is well-formed — correct",
        "missing test for happy path — minor deviation",
    ]


def test_agent_judge_prompt_requires_findings(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    # System prompt is on the AgentConfig — pull it from the construction call.
    (agent_config,) = mock_cls.call_args.args
    sys_prompt = agent_config.system_prompt or ""
    user_msg: str = mock_agent.communicate.call_args.args[0]
    # Verbose system prompt asks for findings.
    assert "findings" in sys_prompt.lower()
    # Analysis is gone — must NOT appear as a required JSON key in either prompt.
    assert '"analysis"' not in sys_prompt
    assert "findings" in user_msg.lower()


def test_agent_judge_transcript_captures_tool_calls(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Tool calls made by the judge sub-agent must surface on the transcript so
    reviewers can audit the verdict."""
    from datetime import datetime

    from coder_eval.models import CommandTelemetry

    cmd1 = CommandTelemetry(
        tool_name="Bash",
        tool_id="t1",
        timestamp=datetime.now(),
        parameters={"command": "xmllint --noout main.xaml"},
        result_status="success",
        result_summary="OK (no errors)",
        sequence_number=0,
    )
    cmd2 = CommandTelemetry(
        tool_name="Read",
        tool_id="t2",
        timestamp=datetime.now(),
        parameters={"file_path": "main.xaml"},
        result_status="success",
        result_summary="File read: 482 bytes",
        sequence_number=1,
    )
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = MagicMock()
    mock_agent.start = AsyncMock(return_value=None)
    mock_agent.communicate = AsyncMock(
        return_value=_make_turn_with_commands('{"score": 0.9, "rationale": "ok"}', [cmd1, cmd2])
    )
    mock_agent.stop = AsyncMock(return_value=None)
    mock_agent.kill = AsyncMock(return_value=None)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert len(transcript.tool_calls) == 2
    assert transcript.tool_calls[0].tool_name == "Bash"
    assert "xmllint" in transcript.tool_calls[0].detail
    assert transcript.tool_calls[0].status == "success"
    assert "OK" in transcript.tool_calls[0].result_preview
    assert transcript.tool_calls[1].tool_name == "Read"
    assert transcript.tool_calls[1].detail == "main.xaml"
    assert transcript.duration_seconds == 2.5


def test_agent_judge_capture_transcript_false_drops_transcript(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade", capture_transcript=False)
    mock_agent = _make_mock_agent('{"score": 0.6, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert getattr(result, "transcript", None) is None
    assert result.score == 0.6


def test_agent_judge_scrubs_reference_from_transcript(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    sentinel = "REF_LEAK_VIA_TRANSCRIPT_777"
    verdict = f'{{"score": 0.7, "rationale": "ok", "findings": ["saw {sentinel}"]}}'
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent(verdict)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert sentinel not in transcript.raw_verdict
    for f in getattr(result, "findings", []) or []:
        assert sentinel not in f


def test_agent_judge_transcript_truncation(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """A long raw verdict gets clipped and the truncated flag flips."""
    big_verdict = '{"score": 0.5, "rationale": "ok", "findings": ["' + "y" * 4000 + '"]}'
    criterion = AgentJudgeCriterion(description="x", prompt="grade", max_transcript_chars=200)
    mock_agent = _make_mock_agent(big_verdict)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert transcript.truncated is True


def test_agent_judge_legacy_two_field_verdict_still_parses(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """A judge that emits the old two-field form (no findings) must still parse cleanly."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.42, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.42
    assert result.error is None
    assert getattr(result, "findings", []) == []


def test_agent_judge_round_trips_through_evaluation_result(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """JudgeCriterionResult fields must survive EvaluationResult.model_dump_json -> model_validate_json.

    extra='allow' on CriterionResult preserves findings/transcript on reload —
    without it, task.json would silently drop the audit data.
    """
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    verdict = '{"score": 0.8, "rationale": "ok", "findings": ["finding A", "finding B"]}'
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent(verdict)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    evaluation = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
        environment_info={},
        success_criteria_results=[result],
    )
    raw_json = evaluation.model_dump_json()
    assert "finding A" in raw_json

    reloaded = EvaluationResult.model_validate_json(raw_json)
    cr = reloaded.success_criteria_results[0]
    assert getattr(cr, "findings", []) == ["finding A", "finding B"]
    # transcript surfaces as a dict-shaped extra after round-trip; the HTML renderer handles both.
    transcript = getattr(cr, "transcript", None)
    assert transcript is not None


def test_agent_judge_timeout_uses_base_criterion_result(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Timeout path returns a base CriterionResult (no transcript / verdict fields)
    because no turn was produced — there's nothing to capture."""
    from coder_eval.errors.timeout import TurnTimeoutError

    criterion = AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=30)
    mock_agent = _make_mock_agent("irrelevant")
    mock_agent.communicate.side_effect = TurnTimeoutError(30.0, task_id="t", iteration=1)

    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.0
    # No transcript captured — the sub-agent never produced a turn.
    assert getattr(result, "transcript", None) is None


# --- enabled flag (master skip) ---


def test_agent_judge_enabled_false_short_circuits(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """enabled=False: no sub-agent spawned, returns a skipped result."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade", enabled=False)
    with patch(_AGENT_PATCH_PATH) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 1.0
    assert "skipped" in (result.details or "").lower()
    assert "enabled=false" in (result.details or "").lower()
    mock_cls.assert_not_called()


def test_agent_judge_enabled_false_works_without_route(sandbox: Sandbox) -> None:
    """A disabled agent_judge should be skippable even when no route is configured —
    that's the point of disabling it under a variant where the backend isn't set up."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade", enabled=False)
    with patch(_AGENT_PATCH_PATH) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 1.0
    assert "skipped" in (result.details or "").lower()
    mock_cls.assert_not_called()


# --- judge prompt + system prompt capture ---


def test_agent_judge_transcript_captures_prompts(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="rubric body ABC")
    mock_agent = _make_mock_agent('{"score": 0.7, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert "rubric body ABC" in transcript.judge_prompt
    assert "GRADING PROMPT:" in transcript.judge_prompt
    assert "strict code reviewer" in transcript.judge_system_prompt.lower()


def test_agent_judge_prompt_capture_scrubs_reference(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    sentinel = "REF_LEAK_VIA_AGENT_PROMPT_222"
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent('{"score": 0.7, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert sentinel in user_msg
    assert sentinel not in transcript.judge_prompt
    assert sentinel not in transcript.judge_system_prompt


# --- reference directory (mounted at _reference/) ---


def test_agent_judge_mounts_reference_dir_when_include_reference_true(
    sandbox: Sandbox, tmp_path: Path, direct_route: DirectRoute
) -> None:
    """include_reference=True + reference_dir set → SubAgentRunner gets the path,
    and the prompt envelope tells the judge to look at _reference/."""
    ref_root = tmp_path / "ref"
    ref_root.mkdir()
    (ref_root / "Main.xaml").write_text("<reference/>")

    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent('{"score": 0.7, "rationale": "ok"}')
    with (
        patch(_AGENT_PATCH_PATH, return_value=mock_agent),
        patch("coder_eval.criteria.agent_judge.SubAgentRunner") as mock_runner_cls,
    ):
        mock_runner = MagicMock()
        mock_runner.run.return_value = _make_turn('{"score": 0.7, "rationale": "ok"}')
        mock_runner_cls.return_value = mock_runner
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, reference_dir=ref_root)

    # Runner was constructed with the reference_dir kwarg.
    runner_kwargs = mock_runner_cls.call_args.kwargs
    assert runner_kwargs["reference_dir"] == ref_root

    # Prompt envelope points the judge at _reference/.
    user_msg = mock_runner.run.call_args.args[0]
    assert "_reference/" in user_msg
    assert "Use Read / Glob / Grep to browse" in user_msg


def test_agent_judge_skips_reference_dir_when_include_reference_false(
    sandbox: Sandbox, tmp_path: Path, direct_route: DirectRoute
) -> None:
    """include_reference=False MUST zero out reference_dir so the judge can't see grading material."""
    ref_root = tmp_path / "ref"
    ref_root.mkdir()
    (ref_root / "Main.xaml").write_text("<reference/>")

    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=False)
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with (
        patch(_AGENT_PATCH_PATH, return_value=mock_agent),
        patch("coder_eval.criteria.agent_judge.SubAgentRunner") as mock_runner_cls,
    ):
        mock_runner = MagicMock()
        mock_runner.run.return_value = _make_turn('{"score": 0.5, "rationale": "ok"}')
        mock_runner_cls.return_value = mock_runner
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, reference_dir=ref_root)

    runner_kwargs = mock_runner_cls.call_args.kwargs
    assert runner_kwargs["reference_dir"] is None
    user_msg = mock_runner.run.call_args.args[0]
    assert "_reference/" not in user_msg


def test_agent_judge_directory_reference_scrubs_file_contents_from_findings(
    sandbox: Sandbox, tmp_path: Path, direct_route: DirectRoute
) -> None:
    """If the judge echoes the content of any reference file in its findings/transcript,
    that content must be scrubbed before persistence — directory reference, multi-file scrub set."""
    ref_root = tmp_path / "ref"
    ref_root.mkdir()
    secret_main = "REF_DIR_SECRET_MAIN_111_AAAAAAAAAA"
    secret_helper = "REF_DIR_SECRET_HELPER_222_BBBBBBBB"
    (ref_root / "Main.xaml").write_text(secret_main)
    (ref_root / "Helper.xaml").write_text(secret_helper)

    # Judge's verdict echoes BOTH file contents in findings + raw_verdict.
    verdict = (
        '{"score": 0.7, "rationale": "ok", "findings": ['
        f'"Main echoes: {secret_main}", '
        f'"Helper echoes: {secret_helper}"'
        "]}"
    )
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent(verdict)
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_dir=ref_root
        )

    findings = getattr(result, "findings", []) or []
    for f in findings:
        assert secret_main not in f
        assert secret_helper not in f
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert secret_main not in transcript.raw_verdict
    assert secret_helper not in transcript.raw_verdict


# --- integration (opt-in, real SDK) ---


@pytest.mark.integration
def test_agent_judge_integration_real_sdk(tmp_path: Path) -> None:
    """Smoke test that actually spawns a Claude Code SDK agent."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-test-"):
        pytest.skip("Needs a real ANTHROPIC_API_KEY to talk to the SDK")

    from coder_eval.models import AgentConfig, SandboxConfig

    (tmp_path / "hello.txt").write_text("Hello, world!\n")
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="agent_judge_integration")
    sb.sandbox_dir = tmp_path

    criterion = AgentJudgeCriterion(
        description="integration smoke",
        prompt=(
            "Check hello.txt in your working directory. If it contains exactly "
            "'Hello, world!' return score=1.0, otherwise 0.0. Reply with ONLY the JSON verdict."
        ),
        max_turns=6,
        turn_timeout=90,
        agent=AgentConfig(
            type="claude-code",
            model="claude-haiku-4-5-20251001",
            allowed_tools=["Read"],
        ),
    )
    result = SuccessChecker(sb, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.error is None, f"integration judge failed: {result.error}\n{result.details}"
    assert result.score >= 0.7


# --- verdict_channel="tool" path ---


def _patch_runner_with_capture(verdict_payload: dict | None, agent_output: str = "Verdict submitted."):
    """Return a patch context manager that swaps ``SubAgentRunner.run`` for a stub
    that populates the runner's ``capture`` and returns a synthetic turn.

    When ``verdict_payload`` is None the capture stays empty (simulates the judge
    failing to call submit_verdict).
    """
    from coder_eval.evaluation.sub_agent import SubAgentRunner
    from coder_eval.models import JudgeVerdict

    def _stub_run(self, user_msg, *, max_turns, turn_timeout):
        if verdict_payload is not None:
            self.capture.verdict = JudgeVerdict.model_validate(verdict_payload)
            self.capture.error = None
            self.capture.called_count += 1
        return _make_turn(agent_output)

    return patch.object(SubAgentRunner, "run", _stub_run)


def test_agent_judge_tool_channel_happy_path(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    payload = {"score": 0.75, "rationale": "ok", "findings": ["a", "b"]}
    with _patch_runner_with_capture(payload):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.75
    assert result.error is None
    assert getattr(result, "findings", []) == ["a", "b"]


def test_agent_judge_tool_channel_did_not_call(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    with _patch_runner_with_capture(None):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"


def test_agent_judge_tool_channel_overwrites_on_retry(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """LAST-call discipline at the criterion layer — the final verdict wins."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")

    def _stub_run(self, user_msg, *, max_turns, turn_timeout):
        # Simulate two calls — final one wins.
        self.capture.verdict = JudgeVerdict(score=0.2, rationale="first")
        self.capture.called_count += 1
        self.capture.verdict = JudgeVerdict(score=0.9, rationale="second")
        self.capture.called_count += 1
        return _make_turn("Verdict submitted.")

    with patch.object(SubAgentRunner, "run", _stub_run):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.9
    assert "second" in (result.details or "")


def test_agent_judge_tool_channel_added_to_allowed_tools() -> None:
    """The submit_verdict MCP tool is force-added to allowed_tools — security-floor contract."""
    from coder_eval.criteria.agent_judge import _build_agent_config

    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    config = _build_agent_config(criterion, system_prompt="sys")
    assert "mcp__coder_eval_judge__submit_verdict" in (config.allowed_tools or [])


def test_agent_judge_tool_channel_transcript_carries_structured_verdict(
    sandbox: Sandbox, direct_route: DirectRoute
) -> None:
    """Tool channel: raw_verdict in the transcript is the JSON-dumped JudgeVerdict, not agent text."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    payload = {"score": 0.42, "rationale": "headline", "findings": ["evidence"]}
    with _patch_runner_with_capture(payload, agent_output="Verdict submitted, stopping."):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert '"score":0.42' in transcript.raw_verdict
    assert '"rationale":"headline"' in transcript.raw_verdict
    assert "Verdict submitted" not in transcript.raw_verdict


def test_agent_judge_tool_channel_transcript_falls_back_to_agent_output_when_no_verdict(
    sandbox: Sandbox, direct_route: DirectRoute
) -> None:
    """When the judge never calls submit_verdict, raw_verdict falls back to the agent's last words."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    with _patch_runner_with_capture(None, agent_output="I considered grading but did not."):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert transcript.raw_verdict == "I considered grading but did not."


def test_agent_judge_timeout_returns_judge_criterion_result(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """A turn timeout produces a properly-typed ``JudgeCriterionResult`` with score=0."""
    from coder_eval.errors.timeout import TurnTimeoutError

    criterion = AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=10)

    def _raise(self, user_msg, *, max_turns, turn_timeout):
        raise TurnTimeoutError(10.0, task_id="t", iteration=1)

    with patch.object(SubAgentRunner, "run", _raise):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert "TurnTimeoutError" in (result.error or "")


def test_agent_judge_rejects_legacy_verdict_channel_field() -> None:
    """YAML still carrying ``verdict_channel`` raises a migration-friendly error."""
    with pytest.raises(ValidationError, match="verdict_channel field was removed"):
        AgentJudgeCriterion.model_validate({"description": "x", "prompt": "grade", "verdict_channel": "text"})


# --- ClaudeCodeAgent + SubAgentRunner forwarding ---


def test_sub_agent_runner_forwards_extra_mcp_servers(tmp_path: Path) -> None:
    """SubAgentRunner stores extra_mcp_servers and forwards them to ClaudeCodeAgent without
    mutating sdk_options (mcp_servers is in _FRAMEWORK_OWNED_SDK_FIELDS)."""
    from coder_eval.evaluation.sub_agent import SubAgentRunner
    from coder_eval.evaluation.verdict_tool import VerdictCapture, build_submit_verdict_mcp_server
    from coder_eval.models import AgentConfig, SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="x")
    sb.sandbox_dir = tmp_path
    capture = VerdictCapture()
    server_name, server = build_submit_verdict_mcp_server(capture)
    cfg = AgentConfig(
        type="claude-code",
        model="claude-haiku-4-5",
        permission_mode="bypassPermissions",
        setting_sources=[],
        allowed_tools=["Read"],
    )
    runner = SubAgentRunner(
        sandbox=sb,
        agent_config=cfg,
        ignore_patterns=[],
        route=DirectRoute(),
        extra_mcp_servers={server_name: server},
        capture=capture,
    )
    assert runner.capture is capture
    # sdk_options must NOT carry mcp_servers
    assert "mcp_servers" not in cfg.sdk_options


def test_claude_code_agent_accepts_extra_mcp_servers() -> None:
    """ClaudeCodeAgent stores extra_mcp_servers; merging into ClaudeAgentOptions is tested live."""
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
    from coder_eval.models import AgentConfig

    cfg = AgentConfig(type="claude-code", model="m", setting_sources=[])
    agent = ClaudeCodeAgent(cfg, extra_mcp_servers={"a": {"type": "sdk", "name": "a", "instance": object()}})
    assert agent._extra_mcp_servers == {
        "a": {"type": "sdk", "name": "a", "instance": agent._extra_mcp_servers["a"]["instance"]}
    }
