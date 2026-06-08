"""Shared logging helpers for agent implementations."""

import logging
import os
from typing import Any


class PrefixedAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """LoggerAdapter that prefixes every record with an ``[instance]`` tag.

    Used to distinguish simultaneous agents in the same run — e.g. ``[coder]``
    for the coding agent and ``[simulator]`` for the tools-disabled
    user-simulator agent — without spinning up a separate logger hierarchy per
    instance.
    """

    def process(self, msg, kwargs):  # type: ignore[override]
        return f"[{self.extra['prefix']}] {msg}", kwargs  # type: ignore[index]


_RAW_SDK_LOG_ENV = "CODER_EVAL_RAW_SDK_LOG"
_TRUTHY = {"1", "true", "yes", "on"}


def raw_sdk_logging_enabled() -> bool:
    """Whether verbatim SDK-event dumping is opted in via ``CODER_EVAL_RAW_SDK_LOG``."""
    return os.environ.get(_RAW_SDK_LOG_ENV, "").strip().lower() in _TRUTHY


def log_raw_sdk_event(
    log: logging.LoggerAdapter,  # type: ignore[type-arg]
    *,
    repr_target: Any,
    attr_target: Any | None = None,
    **header_fields: Any,
) -> None:
    """Dump an SDK event verbatim, the instant it arrives, when opted in.

    Gated behind ``CODER_EVAL_RAW_SDK_LOG`` so normal runs stay quiet. Emits at
    INFO so it shows up in task.log without flipping the whole logger to DEBUG.
    Shared by every agent so the dump format is identical across backends.

    For each event we log, in order:
      * the caller-supplied ``header_fields`` (e.g. ``type=`` for Claude,
        ``method=``/``root_type=`` for Codex),
      * the full ``repr(repr_target)`` (untruncated), and
      * a sorted ``key=value`` dump of every public attribute of
        ``attr_target`` (defaulting to ``repr_target``), so token fields like
        ``usage`` / ``model_usage`` are visible exactly as the SDK delivered
        them even when ``repr`` is terse.

    Args:
        log: The agent's prefixed logger adapter.
        repr_target: The object to ``repr()`` in full.
        attr_target: The object to introspect for the attribute dump. Defaults
            to ``repr_target`` (Codex passes the notification's item root here,
            falling back to the notification when the root is absent).
        header_fields: Arbitrary ``key=value`` pairs rendered into the header
            line, in insertion order.
    """
    if not raw_sdk_logging_enabled():
        return

    try:
        raw_repr = repr(repr_target)
    except Exception as exc:  # pragma: no cover - defensive
        raw_repr = f"<unreprable: {exc!r}>"

    target = repr_target if attr_target is None else attr_target
    attrs: dict[str, Any] = {}
    for name in dir(target):
        if name.startswith("_"):
            continue
        try:
            value = getattr(target, name)
        except Exception as exc:  # pragma: no cover - defensive
            value = f"<unreadable: {exc!r}>"
        if callable(value):
            continue
        attrs[name] = value
    attr_dump = "\n".join(f"    {k} = {v!r}" for k, v in sorted(attrs.items()))

    header = " ".join(f"{k}={v}" for k, v in header_fields.items())
    log.info(
        "RAW_SDK_EVENT %s\n  repr=%s\n  attrs:\n%s",
        header,
        raw_repr,
        attr_dump or "    (none)",
    )
