"""The one list of resolution-time checks every path runs on a fully merged task."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coder_eval.orchestration.early_stop import validate_early_stop
from coder_eval.orchestration.harness_contract import validate_harness_contract
from coder_eval.orchestration.plugin_staging import validate_plugins


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition


def validate_resolved_task(task: TaskDefinition) -> None:
    """Every resolution-time rejection, in raise order: early stop, harness contract, plugins.

    Raises:
        TaskResolutionError: the task asks for something the run cannot honor.
    """
    validate_early_stop(task)
    validate_harness_contract(task)
    validate_plugins(task)
