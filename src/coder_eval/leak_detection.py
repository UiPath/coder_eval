"""Verbatim-leak detection: is a substantive string from one place present in another?

One declaration, THREE consumers pointing in different directions. CE061 asks whether a dataset
row's PROMPT contains a value a criterion grades it on. :func:`coder_eval.optimize.search.
candidate_leaks` asks whether a candidate ``SKILL.md`` newly contains train-row content it should
have generalized. CE057 asks CE061's question one indirection over, of an outcome row against
its ``expectations/<row id>.json``. Same primitive; a second copy would agree on ordinary input
and diverge exactly where either one was written for — so widening `graded_strings` or
`string_leaves` means auditing all THREE call sites.

Its own module rather than three names on ``optimize.gate``: CE061 is a rule about *task files*
and ``optimize.gate`` is the optimize loop's library, so a task-lint test importing from the
optimize gate inverts the dependency. Same separation ``pricing.py`` and ``path_utils.py`` already
have.

**Not reusable here, and worth naming so a later refactor sees both:**
:func:`coder_eval.orchestration.task_loader._substitute_row_in_tree` walks the same
dict/list/str shape, but it MAPS rather than collects.
"""

from __future__ import annotations

from coder_eval.models import BaseSuccessCriterion


# Fields naming WHERE an artifact goes, not WHAT it must contain. A prompt may say
# "write it to .github/workflows/evals.yml" — that removes filename nondeterminism from
# the measurement without revealing the graded behaviour. `skill_name` is a locator for
# the same reason: it names WHICH skill must engage, while the graded thing is the
# engagement EVENT, which no prompt can supply. The outcome pattern this plugin
# prescribes puts the skill name in every prompt by design.
LEAK_LOCATOR_FIELDS = ("path", "agent_file", "file_path", "command", "skill_name")

# Shorter values collide by chance ("ci", "0.7"); a leak worth flagging is a substantive
# string the author put in both places.
LEAK_MIN_CHARS = 12


def string_leaves(node: object) -> list[str]:
    """Every string in a nested dict/list, flattened."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in string_leaves(v)]
    if isinstance(node, list):
        return [s for v in node for s in string_leaves(v)]
    return []


def graded_strings(criterion: BaseSuccessCriterion, *, drop_type: bool) -> list[str]:
    """The substantive strings a criterion asserts CONTENT on.

    Dumps the criterion, drops ``description`` (a label that routinely echoes the scenario and
    grades nothing) and every :data:`LEAK_LOCATOR_FIELDS` key, flattens what is left, and keeps
    values of at least :data:`LEAK_MIN_CHARS`.

    ``drop_type`` additionally removes the discriminator. CE061 leaves it in: a row PROMPT
    containing ``"skill_triggered"`` is itself worth flagging. ``candidate_leaks`` drops it,
    because a skill BODY that discusses eval criteria mentions criterion type names legitimately.
    Measured against this repo: ``skill_triggered`` is the only type name either shipped suite
    contributes at all, and it appears verbatim in FOUR shipped skill bodies — ``check-skill``,
    ``lint-tasks``, ``task`` and ``optimize-skill`` itself. Keeping it would flag every one of
    them, on a discriminator rather than on anything a criterion grades.
    """
    dumped = criterion.model_dump()
    dumped.pop("description", None)
    if drop_type:
        dumped.pop("type", None)
    for locator in LEAK_LOCATOR_FIELDS:
        dumped.pop(locator, None)
    return [value for value in string_leaves(dumped) if len(value) >= LEAK_MIN_CHARS]
