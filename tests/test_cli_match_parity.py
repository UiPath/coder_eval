"""Parity between the two argv-matching surfaces.

``CliMatch`` (a ``record_cli`` response rule's ``when:``) and
``CliCalledCriterion`` declare the same argv facets separately, so each can carry
guidance written for its own job. Separate declarations can drift, and the drift
is silent in the worst direction: a task author stubs a response with a facet the
criterion cannot express, or grades on one no rule can dispatch on, and finds out
only when a suite scores wrong.

The matching *semantics* cannot drift — both lower to one spec dict that
``coder_eval.argv_match`` evaluates — which is what these tests pin.
"""

import pytest
from pydantic import ValidationError

from coder_eval.argv_match import FlagPredicate, MatchSpec, argv_matches
from coder_eval.models import CliCalledCriterion, CliMatch, CliResponse, FlagMatch
from coder_eval.models.cli_match import MATCH_FACET_FIELDS


class TestFacetParity:
    def test_both_surfaces_declare_every_match_facet(self):
        for field in MATCH_FACET_FIELDS:
            assert field in CliMatch.model_fields, f"CliMatch is missing match facet {field!r}"
            assert field in CliCalledCriterion.model_fields, f"cli_called is missing match facet {field!r}"

    def test_the_rule_surface_declares_no_facet_outside_the_shared_tuple(self):
        """Closes the loop the other direction: without this, a facet added to
        CliMatch alone passes, since the loop above only walks MATCH_FACET_FIELDS."""
        assert set(CliMatch.model_fields) == set(MATCH_FACET_FIELDS)

    def test_criterion_adds_only_non_argv_fields(self):
        """A facet on the criterion that CliMatch lacks is a rule authors cannot write."""
        # Everything the criterion adds is about the LOG (where to read, which
        # record, how many), not about the arguments of one invocation.
        non_argv = {"log", "tool", "min_count", "max_count"}
        base = set(CliCalledCriterion.model_fields) - set(MATCH_FACET_FIELDS)
        # Fields inherited from BaseSuccessCriterion are not match surface either.
        from coder_eval.models import BaseSuccessCriterion

        added = base - set(BaseSuccessCriterion.model_fields)
        assert added == non_argv, f"cli_called gained non-facet field(s) {sorted(added - non_argv)}; add to CliMatch"


# One pattern, both surfaces: (pattern, argv, expected verdict).
SHARED_CASES = [
    ({"verb": "ixp dummy1"}, ["ixp", "dummy1"], True),
    ({"verb": "ixp dummy1"}, ["ixp", "dummy2"], False),
    # Prefix semantics: tokens after the verb are unconstrained.
    ({"verb": "ixp dummy1"}, ["ixp", "dummy1", "extra"], True),
    # Token-wise, so a longer word never satisfies a shorter one.
    ({"verb": "projects list"}, ["projects", "lists"], False),
    ({"verb_any_of": ["projects list", "projects get"]}, ["projects", "get", "p1"], True),
    ({"positional": ["proj-1"]}, ["proj-1", "tail"], True),
    ({"verb": "projects get", "positional": ["proj-1"]}, ["projects", "get", "proj-2"], False),
    # Not `output`: the criterion ignores that one by default (see the
    # deliberate-divergence test below), so a shared case cannot use it.
    ({"verb": "projects get", "flags": {"model": "pro"}}, ["projects", "get", "--model", "pro"], True),
    ({"verb": "projects get", "flags": {"model": "pro"}}, ["projects", "get", "--model", "lite"], False),
    ({"flags": {"force": {"present": True}}}, ["delete", "--force", "proj-1"], True),
    ({"flags": {"force": {"absent": True}}}, ["delete", "proj-1"], True),
]


class TestSemanticParity:
    """The same pattern, written on either surface, matches the same argv."""

    @pytest.mark.parametrize(("pattern", "argv", "expected"), SHARED_CASES)
    def test_rule_and_criterion_agree(self, pattern, argv, expected):
        rule_spec = CliMatch.model_validate(pattern).match_spec
        criterion = CliCalledCriterion(description="d", **pattern)
        assert argv_matches(rule_spec, argv) is expected
        assert argv_matches(criterion.match_spec, argv) is expected

    def test_ignore_flags_default_differs_and_that_is_deliberate(self):
        """The criterion drops --output by default; a response rule does not.

        Grading must not depend on a flag that changes nothing about the outcome;
        dispatch may legitimately answer differently for `--output json`.
        """
        assert CliCalledCriterion(description="d", verb="get").ignore_flags == ["output"]
        assert CliMatch(verb="get").ignore_flags == []


class TestSharedValidation:
    """One validator, so a pattern rejected on one surface is rejected on both."""

    @pytest.mark.parametrize("verb", ["ixp projects get --output json", "ixp projects get -o", "delete --yes"])
    def test_a_flag_inside_a_verb_is_rejected_everywhere(self, verb):
        """It validated, then matched nothing: the verb is compared to the NON-flag
        arguments, so the criterion scored 0 against a log holding that very call and
        a response rule fell through to the tool's default."""
        for build in (
            lambda v: CliMatch(verb=v),
            lambda v: CliMatch(verb_any_of=[v]),
            lambda v: CliCalledCriterion(description="d", verb=v),
            lambda v: CliResponse(when={"verb": v}),
        ):
            with pytest.raises(ValidationError, match="looks like a flag"):
                build(verb)

    @pytest.mark.parametrize("verb", ["ixp projects get", "head -1", "seek -1.5"])
    def test_tokens_the_matcher_would_really_see_stay_legal(self, verb):
        """`-1` is a value to the splitter, not a flag, so the check must not forbid it."""
        assert CliMatch(verb=verb).verb_spellings == [verb.split()]
        assert CliCalledCriterion(description="d", verb=verb).verb_spellings == [verb.split()]


class TestLoweredSpecKeys:
    """The lowered spec is a TypedDict, so pyright catches a renamed key. These
    pin what pyright cannot: that the models and the TypedDicts hold the same
    field set, which is what makes `build_match_spec`'s cast honest."""

    def test_flag_predicate_keys_are_exactly_the_model_fields(self):
        assert set(FlagPredicate.__annotations__) == set(FlagMatch.model_fields)

    def test_match_spec_keys_are_exactly_what_lowering_emits(self):
        emitted = set(CliMatch(verb="ixp dummy1").match_spec)
        assert emitted == set(MatchSpec.__annotations__)
        assert emitted == set(CliCalledCriterion(description="d", verb="ixp dummy1").match_spec)
