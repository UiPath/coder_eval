"""Multi-turn user simulation for interactive coding-agent evaluation.

This package adds a dialog loop on top of the single-shot evaluation:
instead of one prompt → one agent response → criteria check, the coding
agent converses with a simulated user (a second LLM driven by persona +
goal) until the task is complete, the simulator emits a stop token, or a
turn / token budget is exhausted.

Public API:
  - ``UserSimulator``  — wraps an LLM invoker (Anthropic direct or AWS
    Bedrock) and produces the next simulated-user utterance.
  - ``SimulatorResult`` — simulator output + token accounting + stop flag.
  - ``DialogStopReason`` — enum for why a dialog ended.
  - ``evaluate_stop`` — pure predicate that decides whether a dialog
    should terminate after a given turn.
"""

from coder_eval.simulation.termination import DialogStopReason, evaluate_stop
from coder_eval.simulation.user_simulator import SimulatorResult, UserSimulator


__all__ = [
    "DialogStopReason",
    "SimulatorResult",
    "UserSimulator",
    "evaluate_stop",
]
