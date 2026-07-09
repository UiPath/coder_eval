"""Live integration tests for claude_settings permission enforcement.

Permission enforcement lives in the Claude Code CLI and is independent of the
model backend, so these tests run against whichever backend the environment is
configured for (DirectRoute via ANTHROPIC_API_KEY, or BedrockRoute via
API_BACKEND=bedrock + AWS_BEARER_TOKEN_BEDROCK / AWS_REGION).

Run:

    pytest -m live tests/test_claude_settings_enforcement_live.py

Three behaviors are validated:

1. A deny rule targeting a specific directory blocks reads of that directory.
2. A narrow deny rule does not clobber reads of unrelated paths — the concern
   raised in PR #174 review.
3. A deny rule on a sibling tree effectively sandboxes the agent.
"""

import tempfile
from pathlib import Path

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.config import Settings
from coder_eval.models import AgentKind, ApiBackend, parse_agent_config
from coder_eval.models.routing import ApiRoute, DirectRoute, resolve_route


pytestmark = [pytest.mark.live]


def _route_from_env() -> ApiRoute:
    """Build the ApiRoute matching the configured backend, same as the orchestrator."""
    settings = Settings()
    if settings.api_backend == ApiBackend.DIRECT:
        return DirectRoute()
    return resolve_route(settings)


def _model_for_env() -> str | None:
    """Return the model string for AgentConfig appropriate to the active backend.

    DirectRoute has no per-route model default, so we pin haiku explicitly to
    keep test runs cheap. BedrockRoute already carries a model resolved from
    BEDROCK_MODEL via ``resolve_route()``; returning None lets that route
    default apply (a hardcoded Anthropic-API model would 400 on Bedrock since
    the cross-region inference profile expects a different ID format).
    """
    settings = Settings()
    if settings.api_backend == ApiBackend.BEDROCK:
        return None
    return "claude-haiku-4-5-20251001"


SECRET_CONTENTS = "TOP_SECRET_MARKER_42"


def _read_calls(turn) -> list:
    """Return Read tool-call telemetry entries from a TurnRecord."""
    return [c for c in turn.commands if c.tool_name == "Read"]


async def _run_single_turn(sandbox_dir: Path, prompt: str, claude_settings: dict) -> tuple[ClaudeCodeAgent, object]:
    """Start an agent in sandbox_dir, run one turn with the given settings."""
    config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        # Intentionally omit allowed_tools: a populated allowed_tools list appears
        # to short-circuit settings-level permissions.deny in the CLI.
        model=_model_for_env(),
        claude_settings=claude_settings,
    )
    agent = ClaudeCodeAgent(config, route=_route_from_env())
    await agent.start(str(sandbox_dir))
    try:
        turn = await agent.communicate(prompt, timeout=60.0, max_turns=3)
    finally:
        await agent.stop()
    return agent, turn


@pytest.mark.asyncio
async def test_deny_blocks_read_of_excluded_directory():
    """A Read(<forbidden_dir>/**) deny rule blocks attempted reads of that dir.

    Validates the "exclude specific directories" use case: the agent can be
    sandboxed further by listing paths it must not touch, even when permission
    mode is acceptEdits.
    """
    with tempfile.TemporaryDirectory() as sandbox_raw, tempfile.TemporaryDirectory() as forbidden_raw:
        # Resolve symlinks (macOS /var → /private/var) so deny globs match the canonical
        # paths the Claude Code CLI checks.
        sandbox = Path(sandbox_raw).resolve()
        forbidden = Path(forbidden_raw).resolve()
        secret_path = forbidden / "secret.txt"
        secret_path.write_text(SECRET_CONTENTS)

        claude_settings = {
            "permissions": {
                "deny": [f"Read({forbidden}/**)"],
            }
        }
        prompt = (
            f"Read the file at the absolute path {secret_path} and print its contents verbatim. "
            "Do not attempt any workaround (no Bash, no shell). Use only the Read tool."
        )

        _, turn = await _run_single_turn(sandbox, prompt, claude_settings)

        read_calls = _read_calls(turn)
        assert read_calls, "Expected the agent to attempt at least one Read tool call"
        # Every Read that targeted the forbidden path must have been denied.
        attempted_forbidden = [c for c in read_calls if str(forbidden) in str(c.parameters)]
        assert attempted_forbidden, (
            f"Agent did not attempt to read forbidden path. Calls: {[(c.tool_name, c.parameters) for c in read_calls]}"
        )
        for call in attempted_forbidden:
            assert call.result_status == "error", (
                f"Expected denied Read of {call.parameters} to report result_status='error', "
                f"got {call.result_status!r}. error_message={call.error_message!r}"
            )

        # The secret must not appear in the agent's reply — defense in depth.
        assert SECRET_CONTENTS not in (turn.agent_output or ""), (
            "Secret contents leaked into agent output despite deny rule"
        )


@pytest.mark.asyncio
async def test_deny_allows_unrelated_reads_in_sandbox():
    """A narrow deny rule does not clobber reads of unrelated paths.

    Validates akshaylive's PR-review concern: passing `claude_settings` with a
    specific `permissions.deny` entry must merge with (not replace) the rest
    of the settings hierarchy — in particular, the agent must still be able
    to Read sandbox files that are not covered by any deny rule.
    """
    with tempfile.TemporaryDirectory() as sandbox_raw, tempfile.TemporaryDirectory() as forbidden_raw:
        sandbox = Path(sandbox_raw).resolve()
        forbidden = Path(forbidden_raw).resolve()
        sandbox_file = sandbox / "note.txt"
        sandbox_file.write_text("sandbox-ok")

        # Co-existing allow + deny in the same dict: validates that both list
        # entries survive the settings-hierarchy merge (akshaylive's concern)
        # and that a narrow deny does not clobber the allow.
        claude_settings = {
            "permissions": {
                "allow": [f"Read({sandbox}/**)"],
                "deny": [f"Read({forbidden}/**)"],
            }
        }
        prompt = (
            f"Read the file at the absolute path {sandbox_file} and print its contents verbatim. "
            "Use only the Read tool."
        )

        _, turn = await _run_single_turn(sandbox, prompt, claude_settings)

        read_calls = _read_calls(turn)
        assert read_calls, "Expected the agent to attempt at least one Read tool call"
        successful_sandbox_reads = [
            c for c in read_calls if str(sandbox_file) in str(c.parameters) and c.result_status == "success"
        ]
        assert successful_sandbox_reads, (
            "Allowed sandbox read was blocked — narrow deny may have clobbered the allow rule. "
            f"Calls: {[(c.parameters, c.result_status, c.error_message) for c in read_calls]}"
        )
        assert "sandbox-ok" in (turn.agent_output or ""), "Agent did not include the sandbox file contents in its reply"


@pytest.mark.asyncio
async def test_broad_deny_limits_agent_to_sandbox():
    """A broad Read(<parent>/**) deny effectively sandboxes the agent.

    Validates the "limit to sandbox only" use case: by denying reads of the
    parent filesystem tree that contains the forbidden fixture, the agent
    cannot escape the sandbox to read sibling directories.
    """
    with tempfile.TemporaryDirectory() as root_raw:
        root = Path(root_raw).resolve()
        sandbox = root / "sandbox"
        sibling = root / "outside"
        sandbox.mkdir()
        sibling.mkdir()
        (sandbox / "inside.txt").write_text("inside-ok")
        (sibling / "leak.txt").write_text(SECRET_CONTENTS)

        # Deny reads under the shared parent, except the sandbox itself is the
        # agent's cwd — the CLI's permission engine will match deny globs
        # against absolute paths, so sibling reads fail while cwd-relative
        # reads of inside.txt stay allowed (no matching deny glob).
        claude_settings = {
            "permissions": {
                "deny": [f"Read({sibling}/**)"],
            }
        }
        prompt = (
            f"Read the file at the absolute path {sibling / 'leak.txt'} and print its contents "
            "verbatim. Use only the Read tool — no Bash, no workarounds."
        )

        _, turn = await _run_single_turn(sandbox, prompt, claude_settings)

        read_calls = _read_calls(turn)
        attempted_outside = [c for c in read_calls if str(sibling) in str(c.parameters)]
        assert attempted_outside, (
            f"Agent did not attempt to read outside the sandbox. "
            f"Calls: {[(c.tool_name, c.parameters) for c in read_calls]}"
        )
        for call in attempted_outside:
            assert call.result_status == "error", (
                f"Expected denied Read of {call.parameters} to report result_status='error', got {call.result_status!r}"
            )
        assert SECRET_CONTENTS not in (turn.agent_output or ""), (
            "Secret contents leaked from sibling directory despite deny rule"
        )
