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

    def test_malformed_line_now_fails_instead_of_being_skipped(self, sandbox_with_log):
        """Superseded behaviour: an unparseable line used to be skipped with the
        score untouched, which let a max_count: 0 guard pass on a truncated record
        of the forbidden call. It is now a harness fault, like a missing log."""
        sandbox, sandbox_dir = sandbox_with_log
        log_path = sandbox_dir / LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(_call(["ixp", "projects", "get", "proj-1"])) + "\nnot json at all\n",
            encoding="utf-8",
        )
        criterion = CliCalledCriterion(description="got it", log=LOG, verb="ixp projects get")
        result = SuccessChecker(sandbox).check(criterion)
        assert result.score == 0.0
        assert "1 unusable record" in (result.error or "")


class TestArgvNormalization:
    def test_equals_form_and_space_form_are_equivalent(self):
        space = _split_flags(["get", "--model", "pro"], frozenset(), frozenset({"model"}))
        equals = _split_flags(["get", "--model=pro"], frozenset(), frozenset({"model"}))
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
        positional, flags = _split_flags(["delete", "proj-1", "--yes", "--force"], frozenset(), frozenset())
        assert positional == ["delete", "proj-1"]
        assert flags == {"yes": [""], "force": [""]}

    def test_flag_like_value_stays_a_value(self):
        """A value that merely looks like a flag is still a value when quoted as one."""
        positional, flags = _split_flags(
            ["confirm", "--corrections", '[{"v":"--x"}]'], frozenset(), frozenset({"corrections"})
        )
        assert positional == ["confirm"]
        assert flags == {"corrections": ['[{"v":"--x"}]']}

    def test_double_dash_terminates_flag_parsing(self):
        """`--` is consumed as a separator; what follows is positional, not a flag."""
        positional, flags = _split_flags(["run", "--", "--not-a-flag"], frozenset(), frozenset())
        assert positional == ["run", "--not-a-flag"]
        assert flags == {}

    def test_lone_dash_is_positional(self):
        """A bare `-` is the stdin convention, not a flag."""
        positional, flags = _split_flags(["import", "-"], frozenset(), frozenset())
        assert positional == ["import", "-"]
        assert flags == {}


class TestRegressionsFromReview:
    """One test per defect found reviewing PR #72, each written in the failing
    direction — the guard that reported a PASS while the log proved otherwise."""

    def test_boolean_switch_before_a_positional_does_not_swallow_it(self, sandbox_with_log):
        """`delete --yes proj-1`: the guard must CATCH the delete, not pass.

        The old heuristic bound `yes=proj-1`, emptied the positionals, and scored
        a `max_count: 0` guard 1.0 on the very invocation it forbids.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "--yes", "proj-1"])])
        forbidden = CliCalledCriterion(
            description="did NOT delete proj-1",
            log=LOG,
            verb="ixp fields delete",
            positional=["proj-1"],
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(forbidden).score == 0.0
        # ...and the positive form of the same assertion must hold.
        positive = forbidden.model_copy(update={"min_count": 1, "max_count": None})
        assert SuccessChecker(sandbox).check(positive).score == 1.0

    def test_ignored_switch_does_not_swallow_the_next_positional(self, sandbox_with_log):
        """Mirror of the --yes case, through ignore_flags.

        Folding ignore_flags into the value-bearing set was right for
        `--output json` but made every ignored SWITCH consume its neighbour,
        reopening the same false PASS on a destructive call.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "--verbose", "proj-1"])])
        guard = CliCalledCriterion(
            description="did NOT delete proj-1",
            log=LOG,
            verb="ixp fields delete",
            positional=["proj-1"],
            ignore_flags=["verbose"],
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(guard).score == 0.0

    def test_ignored_value_flag_still_consumes_its_value(self, sandbox_with_log):
        """The control: `output` is ignored AND declared value-bearing by default,
        so `json` must not leak into the positionals."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "--output", "json", "proj-1"])])
        criterion = CliCalledCriterion(
            description="got proj-1", log=LOG, verb="ixp projects get", positional=["proj-1"]
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_failure_details_show_what_was_actually_recorded(self, sandbox_with_log):
        """A bare count sends the reader to the sandbox; this criterion exists to
        answer 'what did it actually run'."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [
                _call(["ixp", "projects", "get", "proj-1"]),
                _call(["ixp", "projects", "list"]),
                _call(["ixp", "fields", "rename", "proj-1"]),
                _call(["ixp", "track"]),
            ],
        )
        criterion = CliCalledCriterion(description="configured the model", log=LOG, verb="ixp projects configure-model")
        details = SuccessChecker(sandbox).check(criterion).details or ""
        assert "Recorded:" in details
        assert "ixp projects get proj-1" in details
        assert "(+1 more)" in details

    def test_clustered_short_flags_are_split(self, sandbox_with_log):
        """`-yf` used to parse as one flag named `yf`, so an aliases: [y] predicate
        missed it -- leaving the `-y` escape one keystroke away from the hole
        `aliases` exists to close."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "-yf", "proj-1"])])
        guard = CliCalledCriterion(
            description="never deleted without confirming",
            log=LOG,
            verb="ixp fields delete",
            flags={"yes": {"absent": True, "aliases": ["y"]}},
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(guard).score == 1.0

    def test_declared_multi_char_short_flag_is_taken_whole(self):
        """Declaring the name wins over splitting, for CLIs with real -ab flags."""
        assert _split_flags(["rm", "-rf", "p"], frozenset(), frozenset(), frozenset({"rf"})) == (
            ["rm", "p"],
            {"rf": [""]},
        )

    def test_attached_value_on_a_short_flag(self):
        assert _split_flags(["g", "-ff-002"], frozenset(), frozenset({"f"}), frozenset({"f"})) == (
            ["g"],
            {"f": ["f-002"]},
        )

    def test_bare_negative_number_stays_positional(self):
        """`-1` as a flag named `1` dropped it from the positionals -- the same
        silent disappearance as the --yes bug."""
        assert _split_flags(["seek", "-1"], frozenset(), frozenset(), frozenset()) == (
            ["seek", "-1"],
            {},
        )
        assert _split_flags(["seek", "-1.5"], frozenset(), frozenset(), frozenset())[0] == ["seek", "-1.5"]

    def test_declared_numeric_flag_still_parses_as_a_flag(self):
        """`head -1 file` -- declaring it wins over the numeric rule."""
        assert _split_flags(["head", "-1", "f.txt"], frozenset(), frozenset(), frozenset({"1"})) == (
            ["head", "f.txt"],
            {"1": [""]},
        )

    def test_declared_value_flag_consumes_a_dash_leading_value(self):
        """`--limit -1 proj-1`: declared value flags bind even a dash-leading value."""
        positional, flags = _split_flags(
            ["ixp", "proj", "get", "--limit", "-1", "proj-1"], frozenset(), frozenset({"limit"})
        )
        assert positional == ["ixp", "proj", "get", "proj-1"]
        assert flags == {"limit": ["-1"]}

    def test_undeclared_flag_leaves_its_neighbour_positional(self):
        positional, flags = _split_flags(["ixp", "fields", "delete", "--yes", "proj-1"], frozenset(), frozenset())
        assert positional == ["ixp", "fields", "delete", "proj-1"]
        assert flags == {"yes": [""]}

    def test_equals_form_keeps_a_dash_leading_value_and_invents_no_flag(self):
        """`--offset=-1` used to drop the value AND invent a flag named `1`."""
        positional, flags = _split_flags(["get", "--offset=-1"], frozenset(), frozenset())
        assert positional == ["get"]
        assert flags == {"offset": ["-1"]}

    def test_unparseable_line_fails_a_negative_guard(self, sandbox_with_log):
        """An unreadable record might BE the forbidden call, so the guard must fail."""
        sandbox, sandbox_dir = sandbox_with_log
        log_path = sandbox_dir / LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(_call(["ixp", "projects", "get", "p1"]))
            + '\n{"tool": "uip", "argv": ["ixp", "fields", "delete"\n',
            encoding="utf-8",
        )
        forbidden = CliCalledCriterion(
            description="did NOT delete",
            log=LOG,
            verb="ixp fields delete",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(forbidden)
        assert result.score == 0.0
        assert "unusable record" in (result.error or "")

    def test_argv_not_a_list_of_strings_fails_loudly(self, sandbox_with_log):
        """A mock recording argv as a string is a harness fault, not a non-match."""
        sandbox, sandbox_dir = sandbox_with_log
        log_path = sandbox_dir / LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({"tool": "uip", "argv": "ixp fields delete proj-1"}) + "\n", encoding="utf-8")
        forbidden = CliCalledCriterion(
            description="did NOT delete", log=LOG, verb="ixp fields delete", min_count=0, max_count=0
        )
        result = SuccessChecker(sandbox).check(forbidden)
        assert result.score == 0.0
        assert "unusable record" in (result.error or "")

    def test_required_flag_missing_entirely_scores_zero(self, sandbox_with_log):
        """The branch separating `equals` from `absent`, previously uncovered."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "configure-model", "proj-1"])])
        criterion = CliCalledCriterion(
            description="passed --model at all",
            log=LOG,
            verb="ixp projects configure-model",
            flags={"model": "gemini_2_5_pro"},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_asserting_a_switch_does_not_swallow_the_next_positional(self, sandbox_with_log):
        """Adding a switch predicate must not weaken the guard it is added to.

        A presence predicate needs no value, so it must not make the flag
        value-bearing — otherwise `flags: {yes: {present: true}}` on a guard over
        `delete --yes proj-1` would rebind `yes=proj-1`, empty the positionals,
        and hand the guard a false PASS: the exact defect declared value-binding
        exists to prevent, reintroduced by trying to assert more.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "--yes", "proj-1"])])
        guard = CliCalledCriterion(
            description="did NOT delete proj-1 with --yes",
            log=LOG,
            verb="ixp fields delete",
            positional=["proj-1"],
            flags={"yes": {"present": True}},
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(guard).score == 0.0
        positive = guard.model_copy(update={"min_count": 1, "max_count": None})
        assert SuccessChecker(sandbox).check(positive).score == 1.0

    @pytest.mark.parametrize(
        ("argv_tail", "expected"),
        [(["--yes", "proj-1"], 1.0), (["-y", "proj-1"], 1.0), (["proj-1"], 0.0)],
    )
    def test_aliases_make_short_and_long_one_flag(self, sandbox_with_log, argv_tail, expected):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", *argv_tail])])
        criterion = CliCalledCriterion(
            description="confirmed, either spelling",
            log=LOG,
            verb="ixp fields delete",
            flags={"yes": {"present": True, "aliases": ["y"]}},
        )
        assert SuccessChecker(sandbox).check(criterion).score == expected

    @pytest.mark.parametrize(
        ("argv_tail", "expected"),
        [(["--yes", "proj-1"], 1.0), (["-y", "proj-1"], 1.0), (["proj-1"], 0.0)],
    )
    def test_absent_across_aliases_needs_all_spellings_missing(self, sandbox_with_log, argv_tail, expected):
        """Without aliases this was silently wrong: an ANDed pair of absent-guards
        (one per spelling) flagged EVERY invocation, whatever it did, because the
        spelling not used was always absent."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", *argv_tail])])
        guard = CliCalledCriterion(
            description="never deleted without confirming",
            log=LOG,
            verb="ixp fields delete",
            flags={"yes": {"absent": True, "aliases": ["y"]}},
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(guard).score == expected

    def test_alias_of_a_value_flag_binds_its_value(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "labellings", "confirm", "-f", "f-002"])])
        criterion = CliCalledCriterion(
            description="confirmed f-002 via the short flag",
            log=LOG,
            verb="ixp labellings confirm",
            flags={"fields": {"equals": "f-002", "aliases": ["f"]}},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_present_requires_the_flag(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "proj-1"])])
        criterion = CliCalledCriterion(
            description="passed --yes",
            log=LOG,
            verb="ixp fields delete",
            flags={"yes": {"present": True}},
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_bad_regex_flags_value_names_the_flag(self, sandbox_with_log):
        """re.error is not a ValueError, so the pre-flight guard missed this."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "--val", "x"])])
        criterion = CliCalledCriterion(
            description="bad flags int",
            log=LOG,
            verb="ixp projects get",
            flags={"val": {"matches_regex": "a", "flags": 99999999}},
        )
        result = SuccessChecker(sandbox).check(criterion)
        assert result.score == 0.0
        assert "flag 'val'" in (result.error or "")


class TestModelValidation:
    @pytest.mark.parametrize("verb", ["", "   ", "\t"])
    def test_blank_verb_rejected(self, verb):
        """A blank verb is an empty prefix: it matched every record and scored 1.0."""
        with pytest.raises(ValidationError):
            CliCalledCriterion(description="d", log=LOG, verb=verb)

    def test_empty_any_of_rejected(self):
        with pytest.raises(ValidationError):
            CliCalledCriterion(description="d", log=LOG, verb="v", flags={"m": {"any_of": []}})

    def test_min_count_zero_without_max_count_rejected(self):
        """Satisfied by every possible log, so the criterion could never fail."""
        with pytest.raises(ValidationError, match="can never fail"):
            CliCalledCriterion(description="d", log=LOG, verb="v", min_count=0)

    def test_alias_claimed_by_two_predicates_rejected(self):
        """Ambiguous ownership would make the verdict depend on dict order."""
        with pytest.raises(ValidationError, match="claimed by both"):
            CliCalledCriterion(
                description="d",
                log=LOG,
                verb="v",
                flags={"yes": {"present": True, "aliases": ["y"]}, "y": {"present": True}},
            )

    def test_self_alias_rejected(self):
        with pytest.raises(ValidationError, match="itself in aliases"):
            CliCalledCriterion(description="d", log=LOG, verb="v", flags={"yes": {"present": True, "aliases": ["yes"]}})

    def test_alias_in_ignore_flags_rejected(self):
        with pytest.raises(ValidationError, match="ignore_flags"):
            CliCalledCriterion(
                description="d",
                log=LOG,
                verb="v",
                flags={"out": {"present": True, "aliases": ["output"]}},
            )

    def test_predicate_on_an_ignored_flag_rejected(self):
        """ignore_flags drops the flag before predicates run, so this can never work."""
        with pytest.raises(ValidationError, match="ignore_flags"):
            CliCalledCriterion(description="d", log=LOG, verb="v", flags={"output": "json"})

    def test_docstring_negative_example_is_constructible(self):
        """The model docstring's negative example must actually validate."""
        CliCalledCriterion(
            description="Did not use --corrections to flip a boolean field",
            log="mocks/calls.jsonl",
            verb="ixp labellings confirm",
            flags={"corrections": {"contains": "f-100"}},
            min_count=0,
            max_count=0,
        )

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


class TestVerbAlternation:
    """A verb the tool spells several ways, e.g. the old regex's `(list|get)`.

    Without alternation the only way to accept two verbs was to truncate to their
    common prefix, which leaves the following tokens unconstrained — safe for a
    max_count 0 guard, but on a positive assertion it credits `projects delete` as
    readily as `projects get`.
    """

    @pytest.mark.parametrize("subcommand", ["list", "get"])
    def test_any_listed_spelling_matches(self, sandbox_with_log, subcommand):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", subcommand, "proj-1"])])
        criterion = CliCalledCriterion(
            description="read the project",
            log=LOG,
            verb=["ixp projects list", "ixp projects get"],
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_an_unlisted_sibling_does_not_match(self, sandbox_with_log):
        """The point of the feature: `delete` is not silently admitted."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "delete", "proj-1"])])
        criterion = CliCalledCriterion(
            description="read the project",
            log=LOG,
            verb=["ixp projects list", "ixp projects get"],
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_spelling_is_compared_token_by_token(self, sandbox_with_log):
        """`projects list` must not match `projects lists` or `projects list-models`.

        The match is an ordered prefix over TOKENS, not a string startswith, so a
        typo'd or longer-named sibling shares no token with the verb.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(
            sandbox_dir,
            [
                _call(["ixp", "projects", "lists"]),
                _call(["ixp", "projects", "list-models"]),
            ],
        )
        criterion = CliCalledCriterion(description="listed", log=LOG, verb=["ixp projects list"])
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    @pytest.mark.parametrize("subcommand", ["publish", "unpublish"])
    def test_negative_guard_fires_on_every_listed_spelling(self, sandbox_with_log, subcommand):
        """The inverse: a max_count 0 guard must fail on ANY listed verb.

        A change that only widened what scores 1.0 would leave the positive tests
        green while the guard quietly stopped firing.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", subcommand, "proj-1"])])
        criterion = CliCalledCriterion(
            description="did not change published state",
            log=LOG,
            verb=["ixp projects publish", "ixp projects unpublish"],
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_positional_offset_follows_the_matched_spelling(self, sandbox_with_log):
        """Spellings of differing length each measure `positional` from their own end."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "fields", "delete", "proj-1"])])
        criterion = CliCalledCriterion(
            description="deleted from the right project",
            log=LOG,
            verb=["ixp fields remove", "ixp fields delete"],
            positional=["proj-1"],
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_failure_detail_renders_the_alternatives(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "delete", "proj-1"])])
        criterion = CliCalledCriterion(
            description="read the project",
            log=LOG,
            verb=["ixp projects list", "ixp projects get"],
        )
        result = SuccessChecker(sandbox).check(criterion)
        assert "ixp projects list | ixp projects get" in (result.details or "")

    def test_single_verb_detail_is_unchanged(self, sandbox_with_log):
        """A plain string verb renders exactly as it did before this feature."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "delete", "proj-1"])])
        criterion = CliCalledCriterion(description="read", log=LOG, verb="ixp projects get")
        result = SuccessChecker(sandbox).check(criterion)
        assert "verb='ixp projects get'" in (result.details or "")


class TestExactPositional:
    """`positional` is a prefix, so trailing arguments are unconstrained by default.

    That credits a malformed invocation: `verb: 'projects list'` matches
    `projects list dummy`, which the real CLI would reject.
    """

    def test_trailing_arguments_are_accepted_by_default(self, sandbox_with_log):
        """Documents the default, so a change to it fails here rather than silently."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "list", "dummy"])])
        criterion = CliCalledCriterion(description="listed", log=LOG, verb="ixp projects list")
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_exact_positional_rejects_trailing_arguments(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "list", "dummy"])])
        criterion = CliCalledCriterion(
            description="listed",
            log=LOG,
            verb="ixp projects list",
            positional=[],
            exact_positional=True,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_exact_positional_accepts_the_bare_verb(self, sandbox_with_log):
        """The inverse of the above: tightening must not reject the correct call."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "list"])])
        criterion = CliCalledCriterion(
            description="listed",
            log=LOG,
            verb="ixp projects list",
            positional=[],
            exact_positional=True,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_exact_positional_rejects_extra_beyond_a_listed_argument(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "proj-1", "proj-2"])])
        criterion = CliCalledCriterion(
            description="read one project",
            log=LOG,
            verb="ixp projects get",
            positional=["proj-1"],
            exact_positional=True,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_exact_positional_ignores_flags(self, sandbox_with_log):
        """Only NON-flag arguments count, so `--output json` must not break it."""
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "get", "proj-1", "--output", "json"])])
        criterion = CliCalledCriterion(
            description="read one project",
            log=LOG,
            verb="ixp projects get",
            positional=["proj-1"],
            exact_positional=True,
        )
        assert SuccessChecker(sandbox).check(criterion).score == 1.0

    def test_a_negative_guard_is_easier_to_evade_with_exact_positional(self, sandbox_with_log):
        """The asymmetry, asserted so it is visible rather than discovered later.

        Tightening suits a positive assertion. On a max_count 0 guard it works the
        other way: one stray argument stops the match, so the forbidden call slips
        past. Documented on the field; pinned here.
        """
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "delete", "proj-1", "stray"])])
        loose = CliCalledCriterion(
            description="did not delete",
            log=LOG,
            verb="ixp projects delete",
            min_count=0,
            max_count=0,
        )
        tight = CliCalledCriterion(
            description="did not delete",
            log=LOG,
            verb="ixp projects delete",
            positional=["proj-1"],
            exact_positional=True,
            min_count=0,
            max_count=0,
        )
        assert SuccessChecker(sandbox).check(loose).score == 0.0
        assert SuccessChecker(sandbox).check(tight).score == 1.0

    def test_detail_marks_the_match_as_exact(self, sandbox_with_log):
        sandbox, sandbox_dir = sandbox_with_log
        _write_log(sandbox_dir, [_call(["ixp", "projects", "list", "dummy"])])
        criterion = CliCalledCriterion(
            description="listed",
            log=LOG,
            verb="ixp projects list",
            positional=[],
            exact_positional=True,
        )
        result = SuccessChecker(sandbox).check(criterion)
        assert "positional exactly=[]" in (result.details or "")

    def test_exact_positional_without_positional_rejected(self):
        """`positional: []` is the explicit way to say "no arguments"."""
        with pytest.raises(ValidationError, match="requires positional to be set"):
            CliCalledCriterion(
                description="d", log=LOG, verb="ixp projects list", exact_positional=True
            )


class TestVerbAlternationValidation:
    def test_empty_list_rejected(self):
        """`verb: []` is falsy, so it would slip past the at-least-one-facet check."""
        with pytest.raises(ValidationError, match="must not be empty"):
            CliCalledCriterion(description="d", log=LOG, verb=[], positional=["proj-1"])

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_entry_rejected(self, blank):
        with pytest.raises(ValidationError, match="must not be blank"):
            CliCalledCriterion(description="d", log=LOG, verb=["ixp projects get", blank])

    def test_spelling_that_is_a_prefix_of_another_rejected(self):
        """Both match while consuming different token counts, so the `positional`
        offset would depend on list order."""
        with pytest.raises(ValidationError, match="is a prefix of"):
            CliCalledCriterion(description="d", log=LOG, verb=["ixp projects", "ixp projects list"])

    def test_duplicate_spellings_rejected(self):
        """A duplicate is a prefix of itself, caught by the same rule."""
        with pytest.raises(ValidationError, match="is a prefix of"):
            CliCalledCriterion(description="d", log=LOG, verb=["ixp projects get", "ixp projects get"])
