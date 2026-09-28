"""Registration and pricing self-consistency tests for the Delegate agent.

Registry dispatch (`AgentRegistry.get`, `create_agent`) is exercised in
`tests/test_delegate_agent.py::TestRegistration`; this file covers what that
one doesn't: `parse_agent_config` end-to-end dispatch, and the pricing
table's self-consistency (mirroring coder_eval_uipath's pinned-dollar
pattern, which exists specifically to catch a positional cache_write/
cache_read transposition that a self-consistency check against a model's
own input rate alone would not).
"""

from __future__ import annotations

from coder_eval.models import DelegateAgentConfig, parse_agent_config
from coder_eval.pricing import DELEGATE_MODEL_IDS, calculate_cost


# Every id in DELEGATE_MODEL_IDS, paired with its (input+output, cache_write)
# dollar figures at 1M tokens each -- pinned independently of pricing.py so a
# transposed constructor argument still fails this test even though it would
# leave calculate_cost self-consistent against the model's own input_per_mtok.
EXPECTED_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (input + output, cache_write)
    "virtuoso-1-5": (0.95 + 4.0, 0.0),
    "virtuoso-2-0": (0.95 + 4.0, 0.0),
    "gemini-3-5-flash": (1.50 + 9.0, 0.0),
    "gemini-3-6-flash": (1.50 + 7.50, 0.0),
    "gemini-3-1-pro-preview": (2.0 + 12.0, 0.0),
    "gpt-5-4": (2.50 + 15.0, 15.0),
    "gpt-5-5": (5.0 + 30.0, 30.0),
    "gpt-5-6-sol": (5.0 + 30.0, 6.25),
    "gpt-5-6-terra": (2.0 + 12.0, 2.50),
    "gpt-5-6-luna": (0.20 + 1.20, 0.25),
    "kimi-k2-7-code": (0.95 + 4.0, 0.0),
    "glm-5-3-flash": (0.15 + 0.50, 0.0),
}


def test_parse_agent_config_dispatches_to_delegate():
    cfg = parse_agent_config(type="delegate", model="virtuoso-1-5")
    assert isinstance(cfg, DelegateAgentConfig)
    assert cfg.model == "virtuoso-1-5"


def test_expected_models_match_delegate_model_ids_exactly():
    """Forces a new DELEGATE_MODEL_IDS entry to add its own pinned figures
    here, rather than silently going unverified -- unlike a hand-copied
    literal, DELEGATE_MODEL_IDS is derived from pricing.py itself, so this
    genuinely fails the moment a row is added to _DELEGATE_PRICING without a
    matching entry here."""
    assert set(EXPECTED_USD_PER_MTOK) == DELEGATE_MODEL_IDS


def test_every_model_priced_for_input_and_output():
    for model, (expected_io, _) in EXPECTED_USD_PER_MTOK.items():
        cost = calculate_cost(model, 1_000_000, 1_000_000)
        assert cost == expected_io, f"{model}: expected ${expected_io} for input+output, got {cost}"


def test_every_model_priced_for_cache_write():
    """Catches a positional cache_write/cache_read transposition that
    test_every_model_priced_for_input_and_output cannot see."""
    for model, (_, expected_write) in EXPECTED_USD_PER_MTOK.items():
        cost = calculate_cost(model, 0, 0, cache_creation_tokens=1_000_000)
        assert cost == expected_write, f"{model}: expected ${expected_write} for cache-write, got {cost}"
