"""Unit tests for the shared verbatim-leak primitive (`coder_eval.leak_detection`).

The primitive has two consumers pointing in opposite directions — CE036 over a dataset row's
prompt, `optimize_search.candidate_leaks` over a candidate skill body — so these tests exercise the
extraction ITSELF rather than either containment direction. The one behavioural difference between
the consumers is `drop_type`, and it gets its own test.
"""

from __future__ import annotations

import pytest

from coder_eval.leak_detection import LEAK_LOCATOR_FIELDS, LEAK_MIN_CHARS, graded_strings, string_leaves
from coder_eval.models import (
    BaseSuccessCriterion,
    FileCheckCriterion,
    ReferenceComparisonCriterion,
    RunCommandCriterion,
    SkillTriggeredCriterion,
)


# One criterion type per exempt locator, each a type that really declares that field. Derived
# assertions below key off this, so a locator with no carrier is visible rather than skipped.
_CARRIERS: dict[str, BaseSuccessCriterion] = {
    "path": FileCheckCriterion(description="d", path="out.yml"),
    "command": RunCommandCriterion(description="d", command="echo hi"),
    "agent_file": ReferenceComparisonCriterion(description="d", agent_file="agent.py"),
    "skill_name": SkillTriggeredCriterion(description="d", skill_name="my-skill", expected_skill=""),
}


class TestStringLeaves:
    def test_a_bare_string_is_its_own_leaf(self) -> None:
        assert string_leaves("hello") == ["hello"]

    def test_recurses_through_dicts_and_lists(self) -> None:
        node = {"a": "one", "b": ["two", {"c": "three"}], "d": {"e": ["four"]}}
        assert sorted(string_leaves(node)) == ["four", "one", "three", "two"]

    @pytest.mark.parametrize("node", [None, 12, 0.5, True, {}, []])
    def test_scalars_and_empties_yield_nothing(self, node: object) -> None:
        assert string_leaves(node) == []

    def test_dict_keys_are_not_leaves(self) -> None:
        # Only VALUES are content. A key is schema, and scanning keys would flag every criterion
        # in the repo on its own field names.
        assert string_leaves({"a-long-field-name": 3}) == []


class TestGradedStrings:
    def _criterion(self, **overrides) -> FileCheckCriterion:
        base = {"description": "d", "path": "out.yml", "includes": ["minimum-task-score"]}
        return FileCheckCriterion(**{**base, **overrides})

    def test_keeps_a_substantive_asserted_string(self) -> None:
        assert "minimum-task-score" in graded_strings(self._criterion(), drop_type=False)

    def test_drops_the_description(self) -> None:
        # A label. It routinely echoes the scenario and grades nothing.
        label = "the deployment manifest this row asks for"
        assert label not in graded_strings(self._criterion(description=label), drop_type=False)

    @pytest.mark.parametrize("locator", sorted(_CARRIERS))
    def test_drops_every_locator_field(self, locator: str) -> None:
        """Naming WHERE an artifact goes reveals nothing about WHAT it must contain.

        Each locator is exercised on a criterion type that really declares it, so this is an
        assertion rather than a skip.
        """
        value = "x" * LEAK_MIN_CHARS
        criterion = _CARRIERS[locator]
        assert value not in graded_strings(criterion.model_copy(update={locator: value}), drop_type=False)

    def test_every_exempt_locator_is_a_real_field_somewhere(self) -> None:
        """An exemption for a field nothing declares is dead: it weakens the rule's stated scope
        while protecting nothing, and it reads to a maintainer as though some criterion has it.

        Pinned as a SET rather than asserted empty, because `file_path` is dead today and removing
        it changes CE036's shipped exemption list plus its derived CLAUDE.md sentence — a separate
        decision from this module's extraction. This fails if a new dead entry appears, or if a
        criterion grows `file_path` and the pin stops being true.
        """
        assert set(LEAK_LOCATOR_FIELDS) - set(_CARRIERS) == {"file_path"}

    def test_keeps_nested_list_entries(self) -> None:
        # The commonest leak shape in this repo's suites is the SECOND entry of an `includes:`.
        found = graded_strings(self._criterion(includes=["harmless-xx", "permissions-boundary"]), drop_type=False)
        assert "permissions-boundary" in found

    @pytest.mark.parametrize(("delta", "kept"), [(-1, False), (0, True), (1, True)])
    def test_the_length_floor_is_inclusive(self, delta: int, kept: bool) -> None:
        # Both sides of the `>=`. The strings are DERIVED from LEAK_MIN_CHARS rather than spelled
        # out — a hardcoded 11 and 12 would be a second declaration of the same number.
        value = "x" * (LEAK_MIN_CHARS + delta)
        assert (value in graded_strings(self._criterion(includes=[value]), drop_type=False)) is kept


class TestDropType:
    """The ONE behavioural difference between the two consumers, tested from both sides."""

    def _criterion(self) -> SkillTriggeredCriterion:
        return SkillTriggeredCriterion(description="d", skill_name="my-skill", expected_skill="")

    def test_false_retains_the_discriminator(self) -> None:
        # CE036's setting: a row PROMPT containing "skill_triggered" is itself worth flagging.
        assert "skill_triggered" in graded_strings(self._criterion(), drop_type=False)

    def test_true_omits_the_discriminator(self) -> None:
        # `candidate_leaks`' setting. Measured: without it, `optimize-skill`'s own body — which
        # discusses eval criteria at length — flags on `skill_triggered` and on nothing else.
        assert "skill_triggered" not in graded_strings(self._criterion(), drop_type=True)

    def test_the_discriminator_reaches_the_floor(self) -> None:
        # If it were shorter than LEAK_MIN_CHARS the two tests above would pass vacuously.
        assert len("skill_triggered") >= LEAK_MIN_CHARS
