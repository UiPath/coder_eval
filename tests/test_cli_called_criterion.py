"""Tests for CliCalledCriterion — structured matching over an invocation log."""

import json
import re

import pytest
from pydantic import ValidationError

from coder_eval.criteria.cli_called import _split_flags
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import CliCalledCriterion, SandboxConfig
from coder_eval.sandbox import Sandbox


LOG = "mocks/calls.jsonl"


def _write_log(sandbox_dir, records: list[dict]) -> None:
    """Write an invocation log in the shape a recording CLI mock produces."""
    log_path = sandbox_dir / LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _call(argv: list[str], tool: str = "uip", exit_code: int = 1) -> dict:
    return {"ts": 1785416844.987, "tool": tool, "argv": argv, "exit": exit_code}


@pytest.fixture
def sandbox_with_log(request):
    """A tempdir sandbox whose log is populated per-test, then cleaned up."""
    config = SandboxConfig(driver="tempdir", python=None)
    # Sanitize: parametrized test names carry [] {} " which are illegal in a
    # Windows path, and the tempdir driver builds the sandbox dir from task_id.
    safe_task_id = re.sub(r"[^A-Za-z0-9_]", "_", request.node.name)[:50]
    sandbox = Sandbox(config, task_id=safe_task_id)
    sandbox_dir = sandbox.setup()
    yield sandbox, sandbox_dir
    sandbox.cleanup(preserve=False)


class TestVerbAndFlagMatching:
    def test_matches_verb_and_flag_value(self, sandbox_with_log):
        """The canonical positive: verb chain plus one flag value."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "projects", "configure-model", "proj-1", "--model", "gemini_2_5_pro"])],
        )
        criterion = CliCalledCriterion(
            description="switched model",
            log=LOG,
            verb="ixp projects configure-model",
            positional=["proj-1"],
            flags={"model": "gemini_2_5_pro"},
        )
        result = SuccessChecker(sandbox).check(criterion)
        assert result.score == 1.0
        assert result.error is None

    def test_wrong_flag_value_does_not_match(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "projects", "configure-model", "proj-1", "--model", "gemini_2_5_flash"])],
        )
        criterion = CliCalledCriterion(
            description="switched model",
            log=LOG,
            verb="ixp projects configure-model",
            flags={"model": "gemini_2_5_pro"},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_verb_prefix_is_ordered_not_a_token_subset(self, sandbox_with_log):
        """`labellings confirm` must NOT be satisfied by `labellings unconfirm`.

        This is the property that distinguishes an assertion matcher from a
        permissive dispatch matcher.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "labellings", "unconfirm", "proj-1", "doc-1"])])
        criterion = CliCalledCriterion(
            description="confirmed",
            log=LOG,
            verb="ixp labellings confirm",
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_positional_must_follow_the_verb_in_order(self, sandbox_with_log):
        """A value in the wrong position must not satisfy a positional match."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "labellings", "confirm", "doc-1", "proj-1"])])
        criterion = CliCalledCriterion(
            description="confirmed the right project",
            log=LOG,
            verb="ixp labellings confirm",
            positional=["proj-1", "doc-1"],
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_unlisted_flags_are_ignored(self, sandbox_with_log):
        """Extra flags the criterion does not mention never break a match."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "projects", "get", "proj-1", "--verbose", "--folder", "F"])],
        )
        criterion = CliCalledCriterion(description="got it", log=LOG, verb="ixp projects get")
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_tool_filter_separates_shadowed_executables(self, sandbox_with_log):
        """One log serves several mocks; `tool` selects among them."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["-s", "https://example.invalid/projects"], tool="curl")],
        )
        wants_curl = CliCalledCriterion(description="used curl", log=LOG, tool="curl")
        wants_uip = CliCalledCriterion(description="used uip", log=LOG, tool="uip")
        checker = SuccessChecker(sandbox)
        assert checker.check(wants_curl).score == 1.0
        assert checker.check(wants_uip).score == 0.0


class TestFlagPredicates:
    @pytest.mark.parametrize(
        ("predicate", "recorded", "expected"),
        [
            ({"equals": "gemini_2_5_pro"}, "gemini_2_5_pro", 1.0),
            ({"equals": "gemini_2_5_pro"}, "gpt_4o_2024_05_13", 0.0),
            ({"contains": "f-100"}, '[{"field_id":"f-100"}]', 1.0),
            ({"contains": "f-100"}, '[{"field_id":"f-200"}]', 0.0),
            ({"matches_regex": r"^gemini_\d"}, "gemini_2_5_pro", 1.0),
            ({"matches_regex": r"^gemini_\d"}, "gpt_4o", 0.0),
            ({"any_of": ["a", "b"]}, "b", 1.0),
            ({"any_of": ["a", "b"]}, "c", 0.0),
        ],
    )
    def test_predicate_forms(self, sandbox_with_log, predicate, recorded, expected):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "--val", recorded])])
        criterion = CliCalledCriterion(
            description="predicate",
            log=LOG,
            verb="ixp projects get",
            flags={"val": predicate},
        )
        assert SuccessChecker(sandbox).check(criterion).score == expected

    def test_dotall_flag_lets_a_pattern_cross_newlines(self, sandbox_with_log):
        """A heredoc-built payload spans lines; without DOTALL `.` stops at the first."""
        payload = '[\n  {"name": "Invoice Number",\n   "instructions": "Extract it."}\n]'
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "update-prompts", "--updates", payload])])
        pattern = r'"name": "Invoice Number".*"instructions"'

        without_dotall = CliCalledCriterion(
            description="no flags",
            log=LOG,
            verb="ixp fields update-prompts",
            flags={"updates": {"matches_regex": pattern}},
        )
        with_dotall = CliCalledCriterion(
            description="DOTALL",
            log=LOG,
            verb="ixp fields update-prompts",
            flags={"updates": {"matches_regex": pattern, "flags": re.DOTALL}},
        )
        checker = SuccessChecker(sandbox)
        assert checker.check(without_dotall).score == 0.0
        assert checker.check(with_dotall).score == 1.0

    def test_invalid_regex_reports_the_offending_flag(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "--val", "x"])])
        criterion = CliCalledCriterion(
            description="bad pattern",
            log=LOG,
            verb="ixp projects get",
            flags={"val": {"matches_regex": "([unclosed"}},
        )
        result = SuccessChecker(sandbox).check(criterion)
        assert result.score == 0.0
        assert "Invalid matches_regex for flag 'val'" in (result.error or "")

    def test_absent_distinguishes_missing_from_different_value(self, sandbox_with_log):
        """`absent` is why flags is a predicate map, not dict[str, str]."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "labellings", "confirm", "proj-1"])])
        criterion = CliCalledCriterion(
            description="confirmed without corrections",
            log=LOG,
            verb="ixp labellings confirm",
            flags={"corrections": {"absent": True}},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_absent_fails_when_flag_present(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "labellings", "confirm", "proj-1", "--corrections", "[]"])],
        )
        criterion = CliCalledCriterion(
            description="confirmed without corrections",
            log=LOG,
            verb="ixp labellings confirm",
            flags={"corrections": {"absent": True}},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_repeated_flag_satisfied_by_any_value(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "labellings", "confirm", "--fields", "f-1", "--fields", "f-2"])],
        )
        criterion = CliCalledCriterion(
            description="confirmed f-2",
            log=LOG,
            verb="ixp labellings confirm",
            flags={"fields": "f-2"},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0


class TestCounts:
    def test_max_count_zero_is_the_negative_guard(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "rename", "proj-1"])])
        forbidden = CliCalledCriterion(
            description="did not delete the field",
            log=LOG,
            verb="ixp fields delete",
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(forbidden).score == 1.0

    def test_max_count_zero_fails_when_called(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "proj-1"])])
        forbidden = CliCalledCriterion(
            description="did not delete the field",
            log=LOG,
            verb="ixp fields delete",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(forbidden)
        assert result.score == 0.0
        assert "forbids" in result.details

    def test_min_count_requires_repetition(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [_call(["ixp", "documents", "upload", "proj-1", f"doc{n}.pdf"]) for n in range(3)],
        )
        criterion = CliCalledCriterion(
            description="uploaded three documents",
            log=LOG,
            verb="ixp documents upload",
            min_count=3,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0
        stricter = criterion.model_copy(update={"min_count": 4})
        assert SuccessChecker(sandbox).check(stricter).score == 0.0


class TestLogHandling:
    def test_missing_log_fails_even_a_negative_guard(self, sandbox_with_log):
        """A missing log is a harness fault, so `max_count: 0` must NOT pass on it.

        Otherwise re-pointing the mock's sink would make every negative guard
        pass vacuously.
        """
        sandbox, _ = sandbox_with_log
        forbidden = CliCalledCriterion(
            description="did not delete",
            log=LOG,
            verb="ixp fields delete",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(forbidden)
        assert result.score == 0.0
        assert "does not exist" in (result.error or "")

    def test_empty_log_is_zero_calls_not_an_error(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [])
        forbidden = CliCalledCriterion(
            description="did not delete",
            log=LOG,
            verb="ixp fields delete",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(forbidden)
        assert result.score == 1.0
        assert result.error is None

    def test_malformed_lines_are_skipped_and_reported(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        log_path = sandbox_dir / LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(_call(["ixp", "projects", "get", "proj-1"])) + "\nnot json at all\n",
            encoding="utf-8",
        )
        criterion = CliCalledCriterion(description="got it", log=LOG, verb="ixp projects get")
        result = SuccessChecker(sandbox).check(criterion)
        assert result.score == 1.0
        assert "Skipped 1 unparseable" in result.details


class TestArgvNormalization:
    def test_equals_form_and_space_form_are_equivalent(self):
        space = _split_flags(["get", "--model", "pro"], frozenset())
        equals = _split_flags(["get", "--model=pro"], frozenset())
        assert space == equals == (["get"], {"model": ["pro"]})

    def test_output_is_ignored_by_default(self, sandbox_with_log):
        """--output is outcome-invisible, so grading must not depend on it."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "proj-1"])])
        with_json = CliCalledCriterion(
            description="got it",
            log=LOG,
            verb="ixp projects get",
            positional=["proj-1"],
        )
        assert SuccessChecker(sandbox).check(with_json).score == 1.0

        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "--output", "json", "proj-1"])])
        assert SuccessChecker(sandbox).check(with_json).score == 1.0

    def test_boolean_switch_does_not_consume_the_next_flag(self):
        positional, flags = _split_flags(["delete", "proj-1", "--yes", "--force"], frozenset())
        assert positional == ["delete", "proj-1"]
        assert flags == {"yes": [""], "force": [""]}

    def test_flag_like_value_stays_a_value(self):
        """A value that merely looks like a flag is still a value when quoted as one."""
        positional, flags = _split_flags(["confirm", "--corrections", '[{"v":"--x"}]'], frozenset())
        assert positional == ["confirm"]
        assert flags == {"corrections": ['[{"v":"--x"}]']}

    def test_double_dash_terminates_flag_parsing(self):
        """`--` is consumed as a separator; what follows is positional, not a flag."""
        positional, flags = _split_flags(["run", "--", "--not-a-flag"], frozenset())
        assert positional == ["run", "--not-a-flag"]
        assert flags == {}

    def test_lone_dash_is_positional(self):
        """A bare `-` is the stdin convention, not a flag."""
        positional, flags = _split_flags(["import", "-"], frozenset())
        assert positional == ["import", "-"]
        assert flags == {}


class TestModelValidation:
    def test_scalar_shorthand_equals_predicate(self):
        shorthand = CliCalledCriterion(description="d", log=LOG, verb="ixp projects get", flags={"model": "pro"})
        explicit = CliCalledCriterion(
            description="d", log=LOG, verb="ixp projects get", flags={"model": {"equals": "pro"}}
        )
        assert shorthand.flags == explicit.flags

    def test_two_predicates_on_one_flag_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            CliCalledCriterion(
                description="d",
                log=LOG,
                verb="ixp projects get",
                flags={"model": {"equals": "a", "contains": "b"}},
            )

    def test_no_predicate_on_one_flag_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            CliCalledCriterion(description="d", log=LOG, verb="v", flags={"model": {}})

    def test_flags_without_matches_regex_rejected(self):
        """Setting flags beside another predicate would be a silent no-op."""
        with pytest.raises(ValidationError, match="applies only to matches_regex"):
            CliCalledCriterion(description="d", log=LOG, verb="v", flags={"model": {"equals": "x", "flags": 16}})

    def test_max_count_below_min_count_rejected(self):
        with pytest.raises(ValidationError, match="must be >="):
            CliCalledCriterion(description="d", log=LOG, verb="v", min_count=2, max_count=1)

    def test_criterion_with_nothing_to_match_rejected(self):
        """A criterion matching on nothing would count every invocation."""
        with pytest.raises(ValidationError, match="at least one of"):
            CliCalledCriterion(description="d", log=LOG)

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CliCalledCriterion(description="d", log=LOG, verb="v", pattern="oops")
