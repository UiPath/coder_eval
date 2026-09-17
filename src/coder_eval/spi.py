"""The stable import surface for plugin agents.

A plugin imports only from this module and checks ``SPI_VERSION`` in its
``register(registry)`` hook. Any signature change to a name exported here bumps
``SPI_VERSION``; adding a name does not.
"""

from typing import Final

from coder_eval.agent import Agent
from coder_eval.agents._transport import JsonlDecoder, SubprocessJsonlAgent
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import WatchdogFired, run_with_watchdog
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.models import (
    CANONICAL_TOOL_NAMES,
    READ_ONLY_DENIED_TOOLS,
    AgentState,
    ApiRoute,
    BaseAgentConfig,
    CommandTelemetry,
    Enforcement,
    HarnessContract,
    LocalPluginConfig,
    PermissionMode,
    ResultSummary,
    SystemPromptMode,
    TimingBasis,
    TokenUsage,
    ToolNameMap,
    TranscriptMessage,
    TurnRecord,
    UsageGranularity,
)
from coder_eval.pricing import ModelPricing, register_pricing
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.emitter import Generation, TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StopReason,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
    end_status_for,
)
from coder_eval.timing import TurnClock, Window, close_window


SPI_VERSION: Final[int] = 3

__all__ = [  # noqa: RUF022 - plain sort, pinned by tests/test_spi.py
    "Agent",
    "AgentConfigError",
    "AgentCrashError",
    "AgentEndEvent",
    "AgentEndStatus",
    "AgentRegistry",
    "AgentStartEvent",
    "AgentState",
    "ApiRoute",
    "BaseAgentConfig",
    "CANONICAL_TOOL_NAMES",
    "CommandTelemetry",
    "CompositeStreamCallback",
    "Enforcement",
    "EventCollector",
    "Generation",
    "HarnessContract",
    "JsonlDecoder",
    "LocalPluginConfig",
    "ModelPricing",
    "PermissionMode",
    "READ_ONLY_DENIED_TOOLS",
    "ResultSummary",
    "SPI_VERSION",
    "StopReason",
    "StreamCallback",
    "SubprocessJsonlAgent",
    "SystemPromptMode",
    "TextChunkEvent",
    "TimingBasis",
    "TokenUsage",
    "ToolEndEvent",
    "ToolEndStatus",
    "ToolNameMap",
    "ToolStartEvent",
    "TranscriptMessage",
    "TurnClock",
    "TurnEmitter",
    "TurnEndEvent",
    "TurnEndStatus",
    "TurnOutcome",
    "TurnRecord",
    "TurnStartEvent",
    "TurnTimeoutError",
    "UsageGranularity",
    "WatchdogFired",
    "Window",
    "close_window",
    "end_status_for",
    "register_pricing",
    "run_with_watchdog",
]
