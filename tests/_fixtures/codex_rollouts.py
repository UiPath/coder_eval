"""Builders for Codex sub-agent rollout files, shared by the SDK and SDK-free Codex tests.

A rollout is the JSONL a Codex thread writes under ``<CODEX_HOME>/sessions``; the
agent reads a child's rollout to recover what the child ran. The record builders
use the field names of a real CLI 0.156.1 rollout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_LINE_TYPES = {"response_item", "event_msg", "session_meta", "turn_context"}


def write_child_rollout(home: Path, thread_id: str, items: list[dict[str, Any]]) -> Path:
    """Write a child-thread rollout JSONL under ``<home>/sessions`` and return its path.

    The thread id is embedded in the filename verbatim (that's how the agent
    locates it). An entry with a top-level line ``type`` (``event_msg``,
    ``session_meta``, …) is written verbatim; any other entry is a raw ResponseItem
    payload and is wrapped as a ``response_item`` line.
    """
    sessions = home / "sessions" / "2026" / "06" / "05"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-06-05T14-22-36-{thread_id}.jsonl"
    lines = [
        json.dumps(it if it.get("type") in _LINE_TYPES else {"type": "response_item", "payload": it}) for it in items
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def token_count_event(*, inp: int, cached: int, out: int, tot_in: int, tot_cached: int, tot_out: int) -> dict:
    """A child-rollout ``token_count`` event_msg (a generation boundary)."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                "total_token_usage": {
                    "input_tokens": tot_in,
                    "cached_input_tokens": tot_cached,
                    "output_tokens": tot_out,
                },
            },
        },
    }


def exec_call(call_id: str, script: str) -> dict[str, Any]:
    """A code-mode ``exec`` custom tool call."""
    return {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": script}


def exec_output(call_id: str, printed: str = "", *, head: str = "Script completed") -> dict[str, Any]:
    """An ``exec`` call's output: content parts headed by the script's status line, such as ``Script failed``."""
    return {
        "type": "custom_tool_call_output",
        "call_id": call_id,
        "output": [
            {"type": "input_text", "text": f"{head}\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text", "text": printed},
        ],
    }


def wait_call(call_id: str, cell_id: str) -> dict[str, Any]:
    """A code-mode ``wait`` call that resumes the script that yielded this cell id."""
    return {"type": "function_call", "name": "wait", "call_id": call_id, "arguments": json.dumps({"cell_id": cell_id})}


def wait_output(call_id: str, printed: str = "", *, head: str = "Script completed") -> dict[str, Any]:
    """A ``wait`` call's output: headed by the resumed script's status line, as an ``exec`` output is."""
    return {**exec_output(call_id, printed, head=head), "type": "function_call_output"}


def command_record(command: str, *, exit_code: int | None, item_id: str, output: str = "") -> dict[str, Any]:
    """The ``item_completed`` record of one shell command a code-mode script ran."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "id": item_id,
                "command": ["/bin/zsh", "-lc", command],
                "source": "unified_exec_startup",
                "status": "completed" if exit_code == 0 else "failed",
                "aggregated_output": output,
                "exit_code": exit_code,
            },
        },
    }


def file_change_record(path: str, *, item_id: str, status: str = "completed") -> dict[str, Any]:
    """The ``item_completed`` record of a file a code-mode script patched."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "FileChange",
                "id": item_id,
                "changes": {path: {"type": "add", "content": "hi\n"}},
                "status": status,
            },
        },
    }
