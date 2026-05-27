"""Shared logging helpers for agent implementations."""

import logging


class PrefixedAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """LoggerAdapter that prefixes every record with an ``[instance]`` tag.

    Used to distinguish simultaneous agents in the same run — e.g. ``[coder]``
    for the coding agent and ``[simulator]`` for the tools-disabled
    user-simulator agent — without spinning up a separate logger hierarchy per
    instance.
    """

    def process(self, msg, kwargs):  # type: ignore[override]
        return f"[{self.extra['prefix']}] {msg}", kwargs  # type: ignore[index]
