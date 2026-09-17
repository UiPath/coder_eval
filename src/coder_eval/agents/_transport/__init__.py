"""Transport bases shared by more than one harness adapter."""

from coder_eval.agents._transport.subprocess_jsonl import JsonlDecoder, SubprocessJsonlAgent


__all__ = ["JsonlDecoder", "SubprocessJsonlAgent"]
