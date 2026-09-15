"""Duration formatting for the report renderers.

Split out of ``formatting.py`` because that module imports ``claude_agent_sdk``
for the SDK payload formatters, and the reports package should not reach through
an SDK-shaped module for a 14-line duration formatter.

Note what this does NOT buy: ``coder_eval.reports`` still pulls the SDK in
transitively, because ``coder_eval.models.agent_config`` imports
``ClaudeAgentOptions`` and every report module needs ``models``. What is true and
tested (``tests/test_reports_package.py``) is that **this** module is SDK-free, so
the formatter is usable without it, and that ``reports`` no longer imports
``coder_eval.formatting``.
"""

from __future__ import annotations


def format_ms(ms: float | None) -> str:
    """A duration in ms, or an em dash when it was never measured.

    SHARED by the HTML report and the markdown one. They render the same four
    wall-clock buckets from the same `result_metrics.turn_time_buckets` call, so
    formatting them twice is how one surface comes to print `0ms` where the
    other prints a dash — the `None`-vs-`0.0` distinction CE058 enforces on the
    producing side, thrown away at the last step.
    """
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"
