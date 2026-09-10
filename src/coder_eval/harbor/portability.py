"""C1.4 — criteria portability audit for the Harbor export direction.

Not every criterion type can grade truthfully inside a verifier container
that another harness built. This module classifies each of the 15 criterion
types and lets the packager (C2) refuse an unsupported task **at export
time**, where the operator sees why, rather than at verify time, where the
failure is an unexplained low reward with no obvious cause.

Classification, v1:

- ``PORTABLE`` — filesystem/exit-code checks. Nothing about the verifier
  container changes what these need: ``file_exists``, ``file_contains``,
  ``file_matches_regex``, ``json_check``, ``file_check``, ``run_command``,
  ``classification_match``.
- ``NEEDS_REFERENCE`` — ``reference_comparison``. Needs the reference tree,
  which C2 places under ``tests/reference/`` (verifier-side only, never in
  the agent's view — see C2's mapping table).
- ``NEEDS_TRAJECTORY`` — ``command_executed``, ``commands_efficiency``,
  ``skill_triggered``. These read coder-eval's own ``TurnRecord`` iterations.
  In the export direction the verifier is a separate process from the agent
  phase, and the agent may not even be coder-eval (Harbor natively supports
  many agents) — so there is no ``iterations`` list to read from without
  C1.3 (ATIF ingestion + ``evaluate --trajectory``), which is not built yet.
  Hard-error until it is.
- ``NEEDS_CLI_RECORDER`` — ``cli_called``. Reads coder-eval's own JSON Lines
  invocation log (``invocation_log.py``), written by a recorder shim
  coder-eval's OWN sandbox setup installs into `PATH`
  (``_generate_cli_recorders``). The exported Dockerfile does not provision
  that shim. Hard-error until C2 learns to bake it in.
- ``NEEDS_CREDENTIALS`` — ``llm_judge``, ``agent_judge``, ``uipath_eval``.
  Need model credentials and network reachable from inside the verifier
  container, and the judge must not follow the agent's own route (the old
  note's blocking issues). Hard-error in v1, with an explicit opt-in escape
  hatch for an operator who has already provisioned that themselves.
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
