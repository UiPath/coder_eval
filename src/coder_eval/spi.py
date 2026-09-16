"""The stable import surface for plugin agents.

A plugin imports only from this module and checks ``SPI_VERSION`` in its
``register(registry)`` hook. Any signature change to a name exported here bumps
``SPI_VERSION``; adding a name does not.
"""

from typing import Final

from coder_eval.agent import Agent
from coder_eval.agents.registry import AgentRegistry
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
    TokenUsage,
    ToolNameMap,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.pricing import ModelPricing, register_pricing
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.timing import TurnClock, close_window


SPI_VERSION: Final[int] = 1

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
    "HarnessContract",
    "LocalPluginConfig",
    "ModelPricing",
    "PermissionMode",
    "READ_ONLY_DENIED_TOOLS",
    "ResultSummary",
    "SPI_VERSION",
    "StreamCallback",
    "SystemPromptMode",
    "TextChunkEvent",
    "TokenUsage",
    "ToolEndEvent",
    "ToolEndStatus",
    "ToolNameMap",
    "ToolStartEvent",
    "TranscriptMessage",
    "TurnClock",
    "TurnEndEvent",
    "TurnEndStatus",
    "TurnRecord",
    "TurnStartEvent",
    "TurnTimeoutError",
    "close_window",
    "register_pricing",
]
