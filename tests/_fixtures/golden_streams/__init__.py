"""Golden-master characterization fixtures for the agent turn-loops.

A safety net for the ``ClaudeCodeAgent.communicate`` / ``CodexAgent`` turn-loop
decomposition: each scenario replays a recorded SDK event stream through
``communicate()`` and snapshots the resulting ``TurnRecord`` (or, on a
crash/timeout, the ``pending_turn`` partial) as canonical JSON. The decomposition
must keep these snapshots byte-identical post-scrub.

The scrubber masks only the inherently per-run fields (timestamps, durations,
rate-card cost) so token counts, ordering, status, and ``parent_tool_use_id``
stay exact.
"""

from tests._fixtures.golden_streams._scrub import (
    SCRUB_PLACEHOLDER,
    assert_reconciliation,
    scrub,
)


__all__ = ["SCRUB_PLACEHOLDER", "assert_reconciliation", "scrub"]
