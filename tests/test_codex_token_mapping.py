"""SDK-free coverage for the Codex token-mapping contract.

``tests/test_codex_agent.py`` is gated behind ``importorskip("openai_codex")``,
so in CI (where the Codex SDK is absent) none of the Codex token math runs — yet
this is exactly where the uncached/derived-input contract is defined for Codex
(fresh input = ``input - cached``, ``cache_creation = 0`` because OpenAI bills no
separate cache-write fee). ``_fresh_input_tokens`` lives at module scope and the
Codex SDK is imported lazily inside methods, so it (and the resulting
``TokenUsage`` shape) can be exercised without the SDK installed.
"""

from __future__ import annotations

import pytest

from coder_eval.agents.codex_agent import _fresh_input_tokens
from coder_eval.models import TokenUsage


class TestFreshInputTokens:
    """``fresh = max(input - cached, 0)`` — the uncached slice for Codex."""

    @pytest.mark.parametrize(
        ("raw_input", "cached", "expected"),
        [
            (1000, 250, 750),  # typical: prefix served from cache
            (250, 250, 0),  # whole prompt cached → no fresh input
            (100, 250, 0),  # clamped: cached can't exceed input
            (500, 0, 500),  # no cache → all input is fresh
            (0, 0, 0),  # empty
        ],
    )
    def test_fresh_slice(self, raw_input: int, cached: int, expected: int):
        assert _fresh_input_tokens(raw_input, cached) == expected

    def test_never_negative(self):
        # Even with absurd over-reporting of cached, the fresh slice floors at 0.
        assert _fresh_input_tokens(10, 10_000) == 0


class TestCodexTokenUsageContract:
    """The TokenUsage shape Codex builds from a per-generation token_count."""

    def test_fresh_is_uncached_and_cache_creation_is_zero(self):
        # Codex maps: uncached = input - cached, cache_creation = 0 (no write fee),
        # cache_read = cached. The derived total folds them back to the full prompt.
        raw_input, cached, output = 1000, 250, 80
        tu = TokenUsage(
            uncached_input_tokens=_fresh_input_tokens(raw_input, cached),
            output_tokens=output,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
        )
        assert tu.uncached_input_tokens == 750
        assert tu.cache_creation_input_tokens == 0
        assert tu.cache_read_input_tokens == 250
        # Derived total = uncached + cache_creation + cache_read = the full prompt.
        assert tu.input_tokens == 1000
        # Cost bills the uncached slice, never the derived total.
        assert tu.uncached_input_tokens < tu.input_tokens
