"""Golden-master characterization tests for the agent turn-loops.

This is the safety net for decomposing ``ClaudeCodeAgent.communicate`` and
``CodexAgent._run_turn_with_streaming``: each scenario replays a recorded SDK
event stream through ``communicate()`` and asserts the resulting
``TurnRecord`` / ``pending_turn`` is byte-identical (post-scrub) to a committed
JSON snapshot. The decomposition must not change any snapshot.

Regenerate the snapshots after an INTENTIONAL behavior change with::

    GOLDEN_REGEN=1 uv run pytest tests/test_agent_golden_master.py

and review the resulting JSON diff before committing.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests._fixtures.golden_streams import assert_reconciliation, scrub
from tests._fixtures.golden_streams.claude_fixtures import CLAUDE_SCENARIOS, run_claude_scenario


# Codex is an optional extra (mirrors test_codex_agent's guard). Import its
# fixtures only when present so the Claude golden tests in this module still
# collect and run in a base (no-codex) environment instead of erroring at import.
_HAS_CODEX = importlib.util.find_spec("openai_codex") is not None
if _HAS_CODEX:
    from tests._fixtures.golden_streams.codex_fixtures import CODEX_SCENARIOS, run_codex_scenario
else:  # pragma: no cover - only without the optional codex extra
    CODEX_SCENARIOS = []
    run_codex_scenario = None


_EXPECTED_DIR = Path(__file__).parent / "_fixtures" / "golden_streams" / "expected"
_REGEN = os.environ.get("GOLDEN_REGEN", "").strip().lower() in {"1", "true", "yes", "on"}


def _compare_or_regen(name: str, actual_scrubbed: dict[str, Any]) -> None:
    """Compare a scrubbed snapshot to its committed JSON, or regenerate it."""
    path = _EXPECTED_DIR / f"{name}.json"
    serialized = json.dumps(actual_scrubbed, indent=2, sort_keys=True)

    if _REGEN:
        path.write_text(serialized + "\n", encoding="utf-8")
        return

    assert path.exists(), (
        f"Missing golden snapshot {path.name}. Generate it once with "
        f"`GOLDEN_REGEN=1 uv run pytest tests/test_agent_golden_master.py` and commit it."
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual_scrubbed == expected, (
        f"Golden snapshot drift for {name!r}. The turn-loop output changed.\n"
        f"If this change is intentional, regenerate with GOLDEN_REGEN=1 and review the diff."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CLAUDE_SCENARIOS, ids=lambda s: s.name)
async def test_claude_golden(scenario, tmp_path):
    raw = await run_claude_scenario(scenario, str(tmp_path))
    # Reconciliation is asserted on the UNscrubbed dump (token buckets are never
    # scrubbed, but cost/timestamps are — assert before masking to be explicit).
    assert_reconciliation(raw)
    _compare_or_regen(f"claude_{scenario.name}", scrub(raw))


@pytest.mark.skipif(not _HAS_CODEX, reason="openai_codex extra not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CODEX_SCENARIOS, ids=lambda s: s.name)
async def test_codex_golden(scenario, tmp_path):
    raw = await run_codex_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
    _compare_or_regen(f"codex_{scenario.name}", scrub(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CLAUDE_SCENARIOS, ids=lambda s: s.name)
async def test_claude_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Claude snapshot."""
    raw = await run_claude_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)


@pytest.mark.skipif(not _HAS_CODEX, reason="openai_codex extra not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CODEX_SCENARIOS, ids=lambda s: s.name)
async def test_codex_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Codex snapshot."""
    raw = await run_codex_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
