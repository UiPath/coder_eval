"""A stream callback that keeps every event a golden replay emitted."""

from coder_eval.streaming.events import StreamEvent


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)
