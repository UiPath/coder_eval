"""The host→container contract a ``driver: docker`` dispatch stages as ``context.json``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from coder_eval.models.enums import PreservationMode
from coder_eval.models.results import ConfigLineageEntry
from coder_eval.models.sandbox import SandboxConfig


class ContainerContext(BaseModel):
    """What the host asks one container to do.

    Every field is required and unknown keys are refused: a host and an image that
    disagree fail at parse time, never by falling back to a default.

    Rationale: .claude/notes/isolation.md § The container contract
    """

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(description="Experiment variant id; load-bearing for report grouping.")
    replicate_index: StrictInt = Field(
        description="Zero-indexed trial number. Strict, because a bool is an int and True would land in 01/."
    )
    config_lineage: dict[str, ConfigLineageEntry] = Field(
        description="Dotted-path -> the config layer that supplied it. May be empty."
    )
    preservation_mode: PreservationMode = Field(
        description="Resolved host-side from the driver-derived default; the container obeys it."
    )
    grade: StrictBool = Field(
        description=(
            "False is `coder-eval execute`. A run-level CLI decision, deliberately not a task field, "
            "so the container cannot derive it from task.yaml."
        )
    )
    regrade: StrictBool = Field(
        description=(
            "A detached grade: seed from the staged prior.json and adopt CONTAINER_GRADE_WORKSPACE. "
            "Getting it wrong re-RUNS the agent against the workspace it was asked only to grade."
        )
    )
    source_yaml: str = Field(
        description="The host's raw task YAML, so task.json's audit trail matches the in-process driver."
    )
    host_task_file: str | None = Field(
        description=(
            "The HOST's task file path, recorded verbatim into task.json -- distinct from the "
            "container path TASK_DIR resolves against. Null when the task has no file."
        )
    )
    workspace_dir: str | None = Field(
        description=(
            "Docker WORKDIR alignment: the concrete path the agent runs at and is captured from. "
            "Null is the standard run_dir/artifacts workspace."
        )
    )
    authored_sandbox: SandboxConfig = Field(
        description=(
            "The sandbox block as AUTHORED (driver: docker), which task.json records. The staged "
            "task.yaml carries the execution copy the host resolved to driver: tempdir."
        )
    )
