"""Built-in wiring for the Delegate SDK agent: registry binding, pricing, no-auth e2e.

These exercise the agent as the framework sees it — ``register_builtins`` via the
``coder_eval.plugins`` entry point, ``parse_agent_config`` / ``create_agent``
dispatch on the ``delegate-sdk`` kind, the Delegate-routed rows of the pricing
table, and a full ``coder-eval run`` of a Delegate task with the host absent
(which must fail cleanly as ``AGENT_CONFIG_ERROR``). No UiPath auth or Node host
is required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coder_eval.agents.delegate_sdk_agent import DelegateSdkAgent
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.errors.categories import ErrorCategory
from coder_eval.models import AgentKind, DelegateSdkAgentConfig, DirectRoute, parse_agent_config
from coder_eval.plugins import load_plugins
from coder_eval.pricing import _PRICING, calculate_cost


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK = REPO_ROOT / "tasks" / "delegate_sdk_smoke_test.yaml"

# Delegate env vars that must be absent/neutralised for the no-auth e2e to be
# deterministic regardless of the developer's machine.
_DELEGATE_ENV_VARS = (
    "DELEGATE_STDIO_PATH",
    "AUTH_TOKEN",
    "TENANT_ID",
    "ORG_ID",
    "USER_ID",
)


# ---- registration ------------------------------------------------------------


def test_load_plugins_registers_delegate_sdk() -> None:
    """The built-in entry point registers the delegate-sdk agent like every other built-in."""
    load_plugins(force=True)
    registration = AgentRegistry.get(AgentKind.DELEGATE_SDK)
    assert registration is not None
    assert registration.agent_class is DelegateSdkAgent
    assert registration.config_class is DelegateSdkAgentConfig


def test_kind_string_and_enum_member_agree() -> None:
    """``agent.type: delegate-sdk`` in YAML is the same key the enum member registers under."""
    assert AgentKind.DELEGATE_SDK == "delegate-sdk"
    assert AgentRegistry.get("delegate-sdk") is AgentRegistry.get(AgentKind.DELEGATE_SDK)


def test_parse_agent_config_resolves_delegate_config() -> None:
    cfg = parse_agent_config(type="delegate-sdk", model="virtuoso-1-5")
    assert isinstance(cfg, DelegateSdkAgentConfig)
    assert cfg.model == "virtuoso-1-5"
    assert cfg.sdk_options == {}
    assert cfg.project_id == "" and cfg.session_id == ""


def test_delegate_config_accepts_arbitrary_sdk_options_keys() -> None:
    """A shared experiment YAML may carry Claude-only sdk_options keys; the host ignores them."""
    cfg = parse_agent_config(type="delegate-sdk", sdk_options={"effort": "high", "max_thinking_tokens": 4096})
    assert isinstance(cfg, DelegateSdkAgentConfig)
    assert cfg.sdk_options == {"effort": "high", "max_thinking_tokens": 4096}


def test_create_agent_dispatches_to_delegate_sdk_agent() -> None:
    cfg = parse_agent_config(type="delegate-sdk")
    agent = create_agent("delegate-sdk", cfg, route=DirectRoute())
    assert isinstance(agent, DelegateSdkAgent)


def test_environment_info_carries_the_system_prompt_semantics_marker() -> None:
    """CE046 contract: the override spreads the base so the run marker is never absent."""
    agent = DelegateSdkAgent(parse_agent_config(type="delegate-sdk", model="virtuoso-1-5"))  # type: ignore[arg-type]
    info = agent.get_environment_info()
    assert info["system_prompt_semantics"] == "unknown"
    assert info["delegate_model"] == "virtuoso-1-5"


# ---- pricing ------------------------------------------------------------------

# The Delegate route prices on HYPHENATED ids (the agent normalizes the backend's
# ``gpt_5_6_terra`` to ``gpt-5-6-terra``). Hand-written dollar figure for 1M
# tokens in EVERY bucket (uncached input + output + cache write + cache read) —
# an independent second copy of the intended rates, so a transposed field in the
# table (cache_write vs cache_read) cannot ship green.
DELEGATE_ROUTE_ALL_BUCKET_COST: dict[str, float] = {
    "virtuoso-1-5": 0.95 + 4.0 + 0.0 + 0.16,
    "virtuoso-2-0": 0.95 + 4.0 + 0.0 + 0.19,
    "gemini-3-5-flash": 1.50 + 9.0 + 0.0 + 0.15,
    "gemini-3-6-flash": 1.50 + 7.50 + 0.0 + 0.15,
    "gemini-3-1-pro-preview": 2.0 + 12.0 + 0.0 + 0.20,
    "gpt-5-4": 2.50 + 15.0 + 15.0 + 0.25,
    "gpt-5-5": 5.0 + 30.0 + 30.0 + 0.50,
    "gpt-5-6-sol": 5.0 + 30.0 + 6.25 + 0.50,
    "gpt-5-6-terra": 2.0 + 12.0 + 2.50 + 0.20,
    "gpt-5-6-luna": 0.20 + 1.20 + 0.25 + 0.02,
    "kimi-k2-7-code": 0.95 + 4.0 + 0.0 + 0.19,
}


@pytest.mark.parametrize("model", sorted(DELEGATE_ROUTE_ALL_BUCKET_COST))
def test_delegate_route_models_are_in_the_builtin_table(model: str) -> None:
    assert model in _PRICING, f"{model} is not priced; the Delegate agent would report total_cost_usd=None"


@pytest.mark.parametrize("model", sorted(DELEGATE_ROUTE_ALL_BUCKET_COST))
def test_delegate_route_rates_price_all_four_token_buckets(model: str) -> None:
    """1M tokens in each bucket costs the hand-pinned figure (catches transpositions)."""
    cost = calculate_cost(model, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == pytest.approx(DELEGATE_ROUTE_ALL_BUCKET_COST[model])


def test_backend_underscored_id_prices_after_the_agent_normalizes_it() -> None:
    """The agent's ``_``->``-`` normalization is what makes a backend id hit the table."""
    assert calculate_cost("gpt_5_6_terra".replace("_", "-"), 1_000_000, 0) == pytest.approx(2.0)
    assert calculate_cost("gpt_5_6_terra", 1_000_000, 0) is None


def test_gpt_5_6_family_follows_its_documented_cache_ratios() -> None:
    """The 1.25x-input write / 10%-input read claim in pricing.py holds for sol/terra/luna."""
    for model in ("gpt-5-6-sol", "gpt-5-6-terra", "gpt-5-6-luna"):
        rate = _PRICING[model]
        assert rate.cache_write_per_mtok == pytest.approx(1.25 * rate.input_per_mtok), model
        assert rate.cache_read_per_mtok == pytest.approx(0.10 * rate.input_per_mtok), model


# ---- no-auth end-to-end -------------------------------------------------------


def test_no_auth_run_fails_as_agent_config_error(tmp_path: Path) -> None:
    """A full `coder-eval run` of a Delegate task with the host absent must fail
    cleanly as a non-retryable AGENT_CONFIG_ERROR with a saved result.

    Drives the real path: CLI -> ExperimentRunner -> run_batch -> Orchestrator ->
    create_agent('delegate-sdk') -> start() raises AgentConfigError (the bundle is
    forced not-found via a bogus DELEGATE_STDIO_NODE_MODULES) -> categorised ->
    result persisted. Host- and auth-independent, so it runs anywhere.
    """
    run_dir = tmp_path / "run"
    empty_node_modules = tmp_path / "empty_nm"
    empty_node_modules.mkdir()

    env = {**os.environ}
    for var in _DELEGATE_ENV_VARS:
        env.pop(var, None)
    # Point the host resolver at a dir with no node_modules → deterministic
    # bundle-not-found AgentConfigError, before any Node spawn or auth check.
    env["DELEGATE_STDIO_NODE_MODULES"] = str(empty_node_modules)
    # The child's console output carries non-ASCII (the CLI's arrows / em dashes).
    # On Windows a piped stdout defaults to the locale code page, which the utf-8
    # reader below would choke on mid-stream — pin both sides to UTF-8.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", "from coder_eval.cli import app; app()", "run", str(TASK), "--run-dir", str(run_dir)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )

    assert result.returncode != 0, f"expected non-zero exit; stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    task_jsons = list(run_dir.rglob("task.json"))
    assert task_jsons, f"no task.json written under {run_dir}; stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    saved = json.loads(task_jsons[0].read_text(encoding="utf-8"))
    error_details = saved.get("error_details") or {}
    assert error_details.get("error_category") == ErrorCategory.AGENT_CONFIG_ERROR.value
    assert error_details.get("is_retryable") is False
