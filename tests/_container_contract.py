"""A complete, valid ``context.json`` payload for tests that drive the container side."""

from __future__ import annotations

from typing import Any


def contract_payload(*, omit: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
    """Every ``ContainerContext`` field with a valid value, ``overrides`` applied and ``omit`` removed."""
    payload: dict[str, Any] = {
        "variant_id": "default",
        "replicate_index": 0,
        "config_lineage": {},
        "preservation_mode": "DIRECT_WRITE",
        "grade": True,
        "regrade": False,
        "source_yaml": "task_id: t\n",
        "host_task_file": None,
        "workspace_dir": None,
        "authored_sandbox": {"driver": "docker"},
    }
    payload.update(overrides)
    for key in omit:
        del payload[key]
    return payload
