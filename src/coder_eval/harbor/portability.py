"""C1.4 — criteria portability audit for the Harbor export direction.

Not every criterion type can grade truthfully inside a verifier container another
harness built. This module classifies each of the 15 types so the packager can refuse
an unsupported task **at export time**, where the operator sees why.

Classification, v1:

- ``PORTABLE`` — filesystem/exit-code checks.
- ``NEEDS_REFERENCE`` — ``reference_comparison``; never actually blocking, since the
  export always emits ``tests/reference/`` when the task declares one.
- ``NEEDS_TRAJECTORY`` — ``command_executed``, ``commands_efficiency``,
  ``skill_triggered``. Hard-error until ATIF ingestion lands.
- ``NEEDS_CLI_RECORDER`` — ``cli_called``. Hard-error until the export bakes the
  recorder shim in.
- ``NEEDS_CREDENTIALS`` — ``llm_judge``, ``agent_judge``, ``uipath_eval``.
  Hard-error, with an opt-in escape hatch for an operator who has provisioned it.

Rationale: .claude/notes/reporting.md § Not every criterion can grade inside someone else's container
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from coder_eval.models import SuccessCriterion


class CriterionPortability(enum.Enum):
    """How (or whether) a criterion type can grade inside a Harbor verifier container."""

    PORTABLE = "portable"
    NEEDS_REFERENCE = "needs_reference"
    NEEDS_TRAJECTORY = "needs_trajectory"
    NEEDS_CLI_RECORDER = "needs_cli_recorder"
    NEEDS_CREDENTIALS = "needs_credentials"


_PORTABILITY_BY_TYPE: dict[str, CriterionPortability] = {
    "file_exists": CriterionPortability.PORTABLE,
    "file_contains": CriterionPortability.PORTABLE,
    "file_matches_regex": CriterionPortability.PORTABLE,
    "json_check": CriterionPortability.PORTABLE,
    "file_check": CriterionPortability.PORTABLE,
    "run_command": CriterionPortability.PORTABLE,
    "classification_match": CriterionPortability.PORTABLE,
    "reference_comparison": CriterionPortability.NEEDS_REFERENCE,
    "command_executed": CriterionPortability.NEEDS_TRAJECTORY,
    "commands_efficiency": CriterionPortability.NEEDS_TRAJECTORY,
    "skill_triggered": CriterionPortability.NEEDS_TRAJECTORY,
    "cli_called": CriterionPortability.NEEDS_CLI_RECORDER,
    "llm_judge": CriterionPortability.NEEDS_CREDENTIALS,
    "agent_judge": CriterionPortability.NEEDS_CREDENTIALS,
    "uipath_eval": CriterionPortability.NEEDS_CREDENTIALS,
}

# Registry-derived coverage: fails closed on a 16th criterion type added to the
# union without a portability classification, rather than silently exporting
# it as if it were PORTABLE.
_KNOWN_CRITERION_TYPES = frozenset(_PORTABILITY_BY_TYPE)

# Which non-PORTABLE classes v1 refuses to export outright (vs. tolerating
# with a caveat, like NEEDS_REFERENCE — C2 always emits tests/reference/ when
# task.reference is set, so that class is never actually blocking).
_BLOCKING_IN_V1 = frozenset(
    {
        CriterionPortability.NEEDS_TRAJECTORY,
        CriterionPortability.NEEDS_CLI_RECORDER,
        CriterionPortability.NEEDS_CREDENTIALS,
    }
)


class UnknownCriterionTypeError(Exception):
    """A criterion type has no portability classification — fail closed, not open."""


@dataclass(frozen=True)
class PortabilityIssue:
    """One criterion this export cannot grade truthfully inside Harbor's verifier."""

    criterion_description: str
    criterion_type: str
    portability: CriterionPortability


def classify(criterion_type: str) -> CriterionPortability:
    """Look up a single criterion type's portability, raising on an unclassified type."""
    try:
        return _PORTABILITY_BY_TYPE[criterion_type]
    except KeyError:
        raise UnknownCriterionTypeError(
            f"Criterion type {criterion_type!r} has no Harbor-export portability classification "
            + f"(known types: {sorted(_KNOWN_CRITERION_TYPES)}). Classify it in "
            + "coder_eval.harbor.portability before exporting a task that uses it."
        ) from None


def audit_criteria(
    criteria: list[SuccessCriterion],
    *,
    allow_credentials: bool = False,
) -> list[PortabilityIssue]:
    """Return every criterion this v1 export would refuse, or ``[]`` if the task exports cleanly.

    ``allow_credentials`` is the escape hatch for ``NEEDS_CREDENTIALS`` criteria
    (``llm_judge`` / ``agent_judge`` / ``uipath_eval``) — an operator who has
    already provisioned model credentials and network access inside the
    verifier container may pass it to export anyway. It does not affect
    ``NEEDS_TRAJECTORY`` or ``NEEDS_CLI_RECORDER``, which are missing
    functionality (C1.3, a recorder-baking step in C2), not a missing
    permission — no flag can supply what does not exist yet.
    """
    issues: list[PortabilityIssue] = []
    for c in criteria:
        portability = classify(c.type)
        if portability not in _BLOCKING_IN_V1:
            continue
        if portability is CriterionPortability.NEEDS_CREDENTIALS and allow_credentials:
            continue
        issues.append(
            PortabilityIssue(criterion_description=c.description, criterion_type=c.type, portability=portability)
        )
    return issues


__all__ = [
    "CriterionPortability",
    "PortabilityIssue",
    "UnknownCriterionTypeError",
    "audit_criteria",
    "classify",
]
