"""Line-based wire format for streaming events across a process boundary.

Used by the Docker isolation path: the in-container CLI installs a callback
that writes prefixed lines to stdout; ``DockerRunner`` on the host parses them
and forwards to the original host callback. Plain log lines without the prefix
pass through to docker.log unchanged.

Each line is ``LINE_PREFIX`` (the RS-framed sentinel ``"\x1ecoder-eval-stream\x1e:"``)
followed by ``{"cls": <event class name>, "data": <event.model_dump(mode="json")>}``.
The control-char sentinel (not a plain ``STREAM_EVENT:`` token) avoids collisions
with ordinary log output. Datetimes are encoded as ISO strings by Pydantic.
"""

from __future__ import annotations

import contextlib
import json
import logging

from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    CriteriaCheckEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)


logger = logging.getLogger(__name__)

# ASCII Record Separator (U+001E) framing the sentinel makes collision
# with real agent / tool / pytest output effectively impossible: control
# chars below 0x20 don't appear in normal stdout. A line that starts with
# this exact byte sequence is a streamed event by construction.
LINE_PREFIX = "\x1ecoder-eval-stream\x1e:"

_EVENT_CLASSES: dict[str, type[StreamEvent]] = {
    cls.__name__: cls
    for cls in [
        AgentStartEvent,
        TurnStartEvent,
        ToolStartEvent,
        ToolEndEvent,
        TextChunkEvent,
        TurnEndEvent,
        AgentEndEvent,
        CriteriaCheckEvent,
    ]
}


def serialize_event(event: StreamEvent) -> str:
    """Render a single event as a one-line wire string (no trailing newline)."""
    return LINE_PREFIX + json.dumps({"cls": type(event).__name__, "data": event.model_dump(mode="json")})


def has_prefix(line: str) -> bool:
    """Cheap check for whether a line *claims* to be an event.

    Lets callers distinguish "not an event, log as-is" from "parse
    failed, also log as-is but warn" -- the latter is a wire-format
    bug worth surfacing.
    """
    return line.startswith(LINE_PREFIX)


def deserialize_event(line: str) -> StreamEvent | None:
    """Parse a streamed-event line back into a StreamEvent.

    Returns ``None`` for any line without the prefix OR a prefixed line
    that fails to parse. The control-char prefix makes false positives
    practically impossible; a malformed prefixed line is logged at WARN
    so the caller can preserve the raw line in docker.log.
    """
    if not line.startswith(LINE_PREFIX):
        return None
    try:
        payload = json.loads(line[len(LINE_PREFIX) :])
        cls_name = payload["cls"]
        data = payload["data"]
        cls = _EVENT_CLASSES.get(cls_name)
        if cls is None:
            logger.warning("Unknown stream event class %r; dropping", cls_name)
            return None
        # Pydantic handles datetime + nested-model reconstruction from the dict.
        return cls.model_validate(data)
    except Exception as exc:
        # Prefix matched but JSON / class / construction failed. Log loud --
        # this is a wire-format bug, not benign noise.
        logger.warning("Failed to deserialize stream event %r: %s", line[:80], exc)
        return None


class StdoutNDJsonCallback:
    """StreamCallback that writes each event as a ``STREAM_EVENT:`` line to stdout.

    Used inside the Docker container; the host's DockerRunner picks the
    lines back up and forwards them to the host-side callback. Lines are
    flushed immediately so the host sees events in real time.
    """

    def on_event(self, event: StreamEvent) -> None:
        # Plain print + flush -- the in-container Python is line-buffered
        # under non-tty stdout, so explicit flush matters.
        # BrokenPipeError guard: if the host got SIGKILL'd or docker-kill'd
        # the container mid-run, our stdout pipe peer is gone. Letting
        # BrokenPipeError propagate would crash whatever in-container code
        # path emitted the event (typically the orchestrator's run loop)
        # and prevent task.json from being written for partial results.
        with contextlib.suppress(BrokenPipeError, OSError):
            print(serialize_event(event), flush=True)
