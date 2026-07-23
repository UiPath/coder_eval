"""Harbor framework interop (https://www.harborframework.com).

Format-level interoperability with Harbor: vendored ATIF trajectory models
(``atif_models``) and the ``EvaluationResult -> Trajectory`` emitter
(``atif_emit``), so coder-eval runs are consumable by ``harbor view``,
Harbor Hub, and ATIF-based SFT/RL pipelines — with zero runtime dependency
on the ``harbor`` pip package.
"""

from coder_eval.harbor.atif_emit import (
    evaluation_result_to_trajectory,
    write_trajectory_json,
    write_trajectory_json_strict,
)
from coder_eval.harbor.atif_models import (
    ATIF_SCHEMA_VERSION,
    AtifAgent,
    ContentPart,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)


__all__ = [
    "ATIF_SCHEMA_VERSION",
    "AtifAgent",
    "ContentPart",
    "FinalMetrics",
    "Metrics",
    "Observation",
    "ObservationResult",
    "Step",
    "SubagentTrajectoryRef",
    "ToolCall",
    "Trajectory",
    "evaluation_result_to_trajectory",
    "write_trajectory_json",
    "write_trajectory_json_strict",
]
