"""Tests for CommandExecutedCriterion."""

from datetime import datetime

from coder_eval.criteria.command_executed import _MAX_PATTERN_SEARCH_LEN, _match_haystacks, _normalize_shell
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import CommandExecutedCriterion
from coder_eval.models.results import TurnRecord
from coder_eval.models.telemetry import CommandTelemetry


class MockSandbox:
    """Mock sandbox for testing (not used by CommandExecutedChecker but required by SuccessChecker)."""

    def __init__(self):
        self.sandbox_dir = None


def _make_command(
    tool_name: str = "Bash",
    parameters: dict | None = None,
    result_status: str = "success",
    tool_id: str = "tool-1",
) -> CommandTelemetry:
    """Helper to create a CommandTelemetry instance."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=parameters or {},
        result_status=result_status,
    )


def _make_turn(commands: list[CommandTelemetry], iteration: int = 1, crashed: bool = False) -> TurnRecord:
    """Helper to create a TurnRecord with commands."""
    return TurnRecord(
        iteration=iteration,
        user_input="test prompt",
        agent_output="test output",
        commands=commands,
        crashed=crashed,
    )


class TestCommandExecutedCriterion:
    """Test suite for CommandExecutedCriterion."""

    def test_match_found(self):
        """Test matching a Bash curl command with pattern."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://wttr.in/London"},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl to fetch weather",
            tool_name="Bash",
            command_pattern=r"curl.*wttr\.in",
            min_count=1,
            require_success=True,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert result.error is None
        assert "1/1" in result.details

    def test_no_match(self):
        """Test when commands exist but none match the pattern."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "ls -la"},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is None

    def test_no_turn_records(self):
        """Test when turn_records is None."""
        sandbox = MockSandbox()

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=None)

        assert result.score == 0.0
        assert result.error is not None
        assert "turn_records" in result.error

    def test_empty_commands(self):
        """Turns exist but have no commands ⇒ score by the same ``min_count`` math.

        Used to short-circuit on a separate ``"No commands found"`` branch, but
        that branch returned ``0.0`` even when ``min_count=0`` (the negative-
        assertion pattern), which was wrong. Now the empty case falls through
        to the normal scoring math: with ``min_count=1`` and zero matches, the
        score is ``0/1 = 0.0`` and the details mirror the positive shape.
        """
        sandbox = MockSandbox()
        turn_records = [_make_turn(commands=[])]

        criterion = CommandExecutedCriterion(
            description="Agent used any command",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert "0/1 required" in result.details

    def test_tool_name_filter(self):
        """Test that tool_name filter only counts matching tools."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Read", parameters={"file_path": "main.py"}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "python main.py"}, tool_id="t2"),
                    _make_command(tool_name="Read", parameters={"file_path": "test.py"}, tool_id="t3"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used Read tool",
            tool_name="Read",
            min_count=2,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "2/2" in result.details

    def test_require_success(self):
        """Test that require_success filters out failed commands."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://api.example.com"},
                        result_status="error",
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://api.example.com"},
                        result_status="success",
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent successfully used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            require_success=True,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # Only 1 successful curl, need 2
        assert result.score == 0.5

    def test_partial_score(self):
        """Test fractional scoring when min_count > matches found."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "git add ."}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "git commit -m 'test'"}, tool_id="t2"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used git commands",
            tool_name="Bash",
            command_pattern=r"git",
            min_count=3,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # 2 matches out of 3 required
        assert abs(result.score - 2.0 / 3.0) < 0.01

    def test_match_multiline_command(self):
        """`.` must span newlines so backslash-continued commands match."""
        sandbox = MockSandbox()
        multiline_cmd = (
            'uip is resources execute create "salesforce" "Contact" \\\n'
            '  --connection-id "abc-123" \\\n'
            '  --body \'{"LastName": "Smith"}\' \\\n'
            "  --output json"
        )
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": multiline_cmd},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran execute create with --body",
            tool_name="Bash",
            command_pattern=r"uip\s+is\s+resources\s+execute\s+create.*--body",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert result.error is None

    def test_exclude_pattern_multiline(self):
        """`exclude_pattern` should also span newlines (DOTALL) for symmetry."""
        sandbox = MockSandbox()
        help_cmd = "uip foo \\\n  --help"
        real_cmd = "uip foo \\\n  --bar"
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": help_cmd}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": real_cmd}, tool_id="t2"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip foo (not --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+foo",
            exclude_pattern=r"foo.*--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

    def test_invalid_regex(self):
        """Test that invalid regex pattern returns score=0.0 with error."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "ls"}),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Bad regex",
            command_pattern=r"[invalid",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is not None
        assert "Invalid regex" in result.error

    def test_no_filters(self):
        """Test that no tool_name/pattern matches all commands."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Read", parameters={"file_path": "a.py"}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "ls"}, tool_id="t2"),
                    _make_command(tool_name="Write", parameters={"file_path": "b.py"}, tool_id="t3"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent executed any commands",
            min_count=3,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0

    def test_exclude_pattern_filters_help(self):
        """exclude_pattern should skip --help invocations."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --help 2>&1"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --process-key pk1 --feed-id fid1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip maestro flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+maestro\s+flow\s+process\s+get",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

    def test_exclude_pattern_all_excluded(self):
        """If all matches are excluded, score should be 0."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --help"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --help 2>&1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip maestro flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+maestro\s+flow\s+process\s+get",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_exclude_pattern_no_positive_matches(self):
        """exclude_pattern with no positive matches should still yield 0."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "ls -la"}, tool_id="t1"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_exclude_pattern_invalid_regex(self):
        """Invalid exclude_pattern regex should return error."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "uip maestro flow process get pk1"}),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Bad exclude regex",
            command_pattern=r"uip",
            exclude_pattern=r"[invalid",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is not None
        assert "Invalid regex" in result.error

    def test_exclude_pattern_non_bash_tool(self):
        """Test exclude_pattern on non-Bash tool via JSON-serialized parameters."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "output.json", "content": '{"key": "value"}'},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "ignore.json", "content": '{"key": "value"}'},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent wrote a json file but not ignore.json",
            tool_name="Write",
            command_pattern=r"\.json",
            exclude_pattern=r"ignore\.json",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0

    def test_non_bash_tool(self):
        """Test matching non-Bash tool via JSON-serialized parameters."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "output.json", "content": '{"key": "value"}'},
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent wrote output.json",
            tool_name="Write",
            command_pattern=r"output\.json",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0

    def test_non_str_command_does_not_crash_criterion(self):
        """A non-``str`` ``command`` (Codex argv array) must not zero the criterion.

        Codex sub-agent rollout recovery can carry ``command`` as an argv *list*
        (codex_agent.py), which reaches ``CommandTelemetry.parameters`` verbatim.
        Before the ``isinstance`` narrow, the list fell through to
        ``shlex.split(list)`` -> ``AttributeError: 'list' object has no attribute
        'read'``, which aborted ``_matching_commands`` for the entire trajectory
        and scored a pattern-less, otherwise-passing criterion 0.0.
        """
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": ["bash", "-lc", "ls"]}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "echo hi"}, tool_id="t2"),
                ]
            )
        ]
        # Pattern-less: both Bash commands count; the argv-list one must not crash.
        criterion = CommandExecutedCriterion(description="ran bash", tool_name="Bash", min_count=1)
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.error is None
        assert result.score == 1.0
        assert "2/1" in result.details

    def test_non_str_command_still_matches_sibling_by_pattern(self):
        """One argv-list ``command`` must not poison a pattern match on its sibling."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": ["bash", "-lc", "ls"]}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "uip run foo"}, tool_id="t2"),
                ]
            )
        ]
        criterion = CommandExecutedCriterion(
            description="ran uip", tool_name="Bash", command_pattern=r"uip\s+run", min_count=1
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.error is None
        assert result.score == 1.0

    def test_crashed_turn_commands_are_counted(self):
        """Commands from crashed partial turns count toward min_count.

        Scenario: a crash preserves 3 commands; the retry adds 2 more. The
        retry continues from where the crash left off (session-resume + same
        sandbox), so all 5 calls are real executed work and all 5 should
        satisfy a min_count=4 requirement.
        """
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://a"},
                        tool_id="t-crash-1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://b"},
                        tool_id="t-crash-2",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://c"},
                        tool_id="t-crash-3",
                    ),
                ],
                crashed=True,
            ),
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://d"},
                        tool_id="t-clean-1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://e"},
                        tool_id="t-clean-2",
                    ),
                ],
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl at least 4 times",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # All 5 calls (3 from partial + 2 from retry) count → 5/4 capped to 1.0.
        assert result.score == 1.0
        assert "5/4" in result.details

    def test_all_turns_crashed_commands_still_counted(self):
        """Commands from an all-crashed run are real work and must be counted."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://a"},
                        tool_id="t-crash-1",
                    ),
                ],
                crashed=True,
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

    # ------------------------------------------------------------------
    # max_count + min_count=0 negative-assertion patterns.
    # Skills task YAMLs (uipath-skills) use these to express "must NOT call
    # the retired command". Before max_count landed, those YAMLs failed
    # pydantic validation in the `Validate Skills Task YAMLs` CI gate.
    # ------------------------------------------------------------------

    def test_negative_assertion_passes_when_no_match(self):
        """min_count=0, max_count=0 ⇒ pass iff the pattern never matched."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip admin users list --search alice"},
                        tool_id="t-ok-1",
                    ),
                ]
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent did NOT use the retired `uip or users list` path",
            tool_name="Bash",
            command_pattern=r"uip\s+or\s+users\s+list",
            min_count=0,
            max_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "allowed range 0..0" in result.details

    def test_negative_assertion_fails_when_pattern_matched(self):
        """min_count=0, max_count=0 ⇒ a single retired-call match fails the gate."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip or users list"},
                        tool_id="t-retired-1",
                    ),
                ]
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent did NOT use the retired `uip or users list` path",
            tool_name="Bash",
            command_pattern=r"uip\s+or\s+users\s+list",
            min_count=0,
            max_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert "allowed range 0..0" in result.details

    def test_bounded_range_pass_inside(self):
        """min_count=2, max_count=4 ⇒ 3 matches sits inside the range."""
        sandbox = MockSandbox()
        commands = [
            _make_command(
                tool_name="Bash",
                parameters={"command": "curl https://api/x"},
                tool_id=f"t-{i}",
            )
            for i in range(3)
        ]
        turn_records = [_make_turn(commands)]

        criterion = CommandExecutedCriterion(
            description="Agent retried within bounds",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            max_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "allowed range 2..4" in result.details

    def test_bounded_range_fails_when_over_cap(self):
        """min_count=2, max_count=4 ⇒ 5 matches busts the cap (score 0.0)."""
        sandbox = MockSandbox()
        commands = [
            _make_command(
                tool_name="Bash",
                parameters={"command": "curl https://api/x"},
                tool_id=f"t-{i}",
            )
            for i in range(5)
        ]
        turn_records = [_make_turn(commands)]

        criterion = CommandExecutedCriterion(
            description="Agent must not retry more than 4 times",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            max_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_min_count_zero_no_max_is_trivially_satisfied(self):
        """min_count=0 with no max_count ⇒ score 1.0 even when no commands match."""
        sandbox = MockSandbox()
        turn_records = [_make_turn([])]

        criterion = CommandExecutedCriterion(
            description="Optional command (passes vacuously)",
            tool_name="Bash",
            command_pattern=r"never-matches",
            min_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # No turn-records-empty short-circuit: turns exist but commands is [].
        # Score should be 1.0 by the min_count==0 rule, not 0/0 ZeroDivisionError.
        assert result.score == 1.0

    def test_invalid_range_rejected_at_model_level(self):
        """max_count < min_count must be rejected by the Pydantic validator."""
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError, match=r"max_count.*must be >= min_count"):
            CommandExecutedCriterion(
                description="impossible range",
                command_pattern="x",
                min_count=5,
                max_count=2,
            )


class TestNormalizeShell:
    """Unit tests for the shell-normalization helper."""

    def test_unwraps_bash_lc_and_resolves_single_quotes(self):
        raw = (
            '/bin/bash -lc "uip is resources run list uipath-salesforce-slack '
            "'curated_channels?types=public_channel,private_channel' --output json\""
        )
        assert _normalize_shell(raw) == (
            "uip is resources run list uipath-salesforce-slack "
            "curated_channels?types=public_channel,private_channel --output json"
        )

    def test_unwraps_escaped_double_quotes(self):
        raw = '/bin/bash -lc "uip is resources run list \\"slack\\" \\"curated_channels\\""'
        assert _normalize_shell(raw) == "uip is resources run list slack curated_channels"

    def test_bare_command_is_just_requoted(self):
        assert _normalize_shell("uip is resources run list slack 'curated_channels'") == (
            "uip is resources run list slack curated_channels"
        )

    def test_shell_operators_survive_as_tokens(self):
        raw = "uip maestro flow validate X.flow --output json && uip maestro flow format X.flow"
        assert _normalize_shell(raw) == raw  # already unquoted; operators kept verbatim

    def test_unbalanced_quotes_return_none(self):
        assert _normalize_shell("echo 'unterminated") is None

    def test_argv_joined_payload_is_not_collapsed_to_first_word(self):
        """Codex rollout recovery joins argv WITHOUT re-quoting (codex_agent.py).

        The wrapper unwrap must keep every token after ``-lc``, not just the
        first — otherwise ``bash -lc uip is resources ...`` collapses to ``uip``,
        making the fix a silent no-op on the sub-agent path.
        """
        raw = "bash -lc uip is resources run list slack curated_channels --output json"
        assert _normalize_shell(raw) == "uip is resources run list slack curated_channels --output json"

    def test_argv_joined_short_command(self):
        assert _normalize_shell("bash -c echo hi there") == "echo hi there"

    def test_every_wrapper_form_is_unwrapped(self):
        """One case per shell/flag shape the agents emit — the allowlist can't rot.

        The predicate replaced an enumerated allowlist that omitted ``zsh``
        (Codex's shell on macOS, codex_agent.py) and ``-ic`` while listing the
        exotic ``-lic``; on those hosts the normalization silently reverted to
        the pre-fix false-negative behaviour. Each entry must strip the wrapper.
        """
        cases = {
            # zsh — Codex's default login shell on macOS
            '/bin/zsh -lc "uip is resources run list slack curated_channels"': (
                "uip is resources run list slack curated_channels"
            ),
            'zsh -lc "echo hi"': "echo hi",
            'sh -c "echo hi"': "echo hi",
            'dash -c "echo hi"': "echo hi",
            'ksh -c "echo hi"': "echo hi",
            'bash -ic "echo hi"': "echo hi",  # interactive + command
            '/usr/bin/bash -lic "echo hi"': "echo hi",  # login + interactive + command
            "bash -l -c 'echo hi'": "echo hi",  # split login/command flags
        }
        for raw, expected in cases.items():
            assert _normalize_shell(raw) == expected, raw

    def test_non_shell_arg0_is_not_unwrapped(self):
        """A non-shell program (basename not ending in ``sh``) is only re-quoted.

        ``git -c <config>`` is the motivating case: ``-c`` is a real git flag, but
        because ``git`` is not a shell the payload must NOT be unwrapped.
        """
        assert _normalize_shell("git -c user.name=x status") == "git -c user.name=x status"
        assert _normalize_shell("uip is resources run list slack 'curated_channels'") == (
            "uip is resources run list slack curated_channels"
        )

    def test_empty_and_whitespace_input_return_none(self):
        """Empty / whitespace-only input has no tokens -> None (benign, not an error)."""
        assert _normalize_shell("") is None
        assert _normalize_shell("   ") is None

    def test_inner_unbalanced_quotes_return_none(self):
        """A wrapper whose script token can't be re-split falls back to None."""
        # Outer double quotes balance, so the script token is `echo 'unterminated`;
        # re-splitting that raises ValueError on the stray single quote.
        assert _normalize_shell('bash -lc "echo \'unterminated"') is None

    def test_non_wrapper_positional_returns_verbatim(self):
        """A shell with a positional before any -c is a script invocation, not `-c`.

        `bash script.sh -c foo` runs the file `script.sh`; the later `-c` is an
        argument to the script, not a command flag, so nothing is unwrapped.
        """
        assert _normalize_shell("bash script.sh -c foo") == "bash script.sh -c foo"

    def test_shell_with_flags_but_no_command_flag_is_verbatim(self):
        """A shell invoked with only non-``-c`` flags (no command) unwraps nothing.

        The wrapper scan exhausts without finding a command flag or a positional,
        so the tokens are returned as-is.
        """
        assert _normalize_shell("bash --norc -i") == "bash --norc -i"

    def test_is_memoized(self):
        """The hot early-stop path re-scans the trajectory; normalize once per command.

        Counting via ``cache_info`` (not wall-clock) so the guard can't flake.
        """
        _normalize_shell.cache_clear()
        raw = "bash -lc 'echo hello world'"
        first = _normalize_shell(raw)
        second = _normalize_shell(raw)
        assert first == second == "echo hello world"
        assert _normalize_shell.cache_info().hits >= 1  # second call served from cache


class TestShellQuotingNormalization:
    """Patterns match regardless of how the agent quoted the command.

    Regression for ``skill-flow-paginated-reference-lookup``: the agent
    paginated ``uip is resources run list <slack> 'curated_channels?...'``
    correctly (a sibling ``nextPage=`` criterion matched the same calls), but the
    gating pagination criterion's pattern allowed only a bare or ``\\"``-escaped
    token — the agent single-quoted the resource arg — so it scored 0.0 (false
    negative). Normalizing the command before matching fixes the whole class
    without touching any task YAML.
    """

    # The exact recorded shape: `bash -lc "..."` wrapper, resource arg in SINGLE
    # quotes. Second call adds a nextPage token (still single-quoted).
    _PAGE1 = (
        '/bin/bash -lc "uip is resources run list uipath-salesforce-slack '
        "'curated_channels?types=public_channel,private_channel' --connection-id abc --output json\""
    )
    _PAGE2 = (
        '/bin/bash -lc "uip is resources run list uipath-salesforce-slack '
        "'curated_channels?types=public_channel,private_channel' "
        "--query 'nextPage=eyJwYWdlIjoyfQ' --output json\""
    )
    # The ORIGINAL, unchanged pattern from the task YAML: allows an optional
    # backslash + optional DOUBLE quote, but no single quote.
    _YAML_PATTERN = r'uip\s+is\s+resources\s+run\s+list\s+\\?"?uipath-salesforce-slack\\?"?\s+\\?"?curated_channels'

    def test_single_quoted_calls_now_counted_with_original_pattern(self):
        """The unchanged YAML pattern now counts both single-quoted calls."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": self._PAGE1}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": self._PAGE2}, tool_id="t2"),
                ]
            )
        ]
        criterion = CommandExecutedCriterion(
            description="paginated curated_channels list ran >1x",
            tool_name="Bash",
            command_pattern=self._YAML_PATTERN,
            min_count=2,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0
        assert "2/2" in result.details

    def test_escaped_double_quote_form_still_matches(self):
        """Backward compat: the escaping style the pattern anticipated still hits."""
        sandbox = MockSandbox()
        raw = (
            '/bin/bash -lc "uip is resources run list \\"uipath-salesforce-slack\\" '
            '\\"curated_channels?types=x\\" --output json"'
        )
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": raw})])]
        criterion = CommandExecutedCriterion(
            description="curated_channels list ran",
            tool_name="Bash",
            command_pattern=self._YAML_PATTERN,
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_shell_operator_pattern_still_matches(self):
        """`&&`/`|` patterns keep working — operators survive normalization."""
        sandbox = MockSandbox()
        cmd = "/bin/bash -lc 'uip maestro flow validate X.flow --output json && uip maestro flow format X.flow'"
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": cmd})])]
        criterion = CommandExecutedCriterion(
            description="validate then format",
            tool_name="Bash",
            command_pattern=r"uip\s+maestro\s+flow\s+validate.*&&.*uip\s+maestro\s+flow\s+format",
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_unbalanced_quotes_fall_back_to_raw_without_crashing(self):
        """A command shlex can't parse still matches against its raw text."""
        sandbox = MockSandbox()
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": "echo 'unterminated"})])]
        criterion = CommandExecutedCriterion(
            description="echo ran",
            tool_name="Bash",
            command_pattern=r"echo\s+",
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_argv_joined_command_matches_full_pattern(self):
        """The argv-joined (unquoted) shape matches a whole-command pattern.

        This is Codex's sub-agent rollout-recovery telemetry — argv joined with
        spaces and no re-quoting. Before the unwrap fix it collapsed to the first
        word, so this pattern scored 0.0.
        """
        sandbox = MockSandbox()
        cmd = "bash -lc uip is resources run list uipath-salesforce-slack curated_channels --output json"
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": cmd})])]
        criterion = CommandExecutedCriterion(
            description="listed curated_channels",
            tool_name="Bash",
            command_pattern=r"uip\s+is\s+resources\s+run\s+list\s+\S+\s+curated_channels",
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_argv_joined_not_collapsed_into_false_anchored_match(self):
        """The collapse-to-first-word bug could make ``^git$`` match ``git push ...``.

        The real command is ``git push --force origin main``; an anchored
        ``^git$`` negative assertion must PASS (score 1.0). The old code collapsed
        the payload to the single token ``git``, which matched ``^git$`` and
        force-failed the gate for a command the agent never actually ran bare.
        """
        sandbox = MockSandbox()
        cmd = "bash -lc git push --force origin main"
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": cmd})])]
        criterion = CommandExecutedCriterion(
            description="must NOT run bare `git`",
            tool_name="Bash",
            command_pattern=r"^git$",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_bash_record_without_command_is_not_shell_normalized(self):
        """A Bash record with no ``command`` key must not have its JSON params tokenized.

        ``_match_haystacks`` is now told whether cmd_text is a shell command
        (``is_shell``, decided once at the extraction site) instead of
        re-deriving it from ``tool_name == "Bash"`` alone. A Bash record whose
        ``command`` is missing (reachable on the Codex path) falls back to the
        JSON blob and must NOT be normalized — otherwise shlex strips the JSON
        quotes, adding a haystack that can newly satisfy an ``exclude_pattern``
        and wrongly drop the command below ``min_count``.
        """
        sandbox = MockSandbox()
        # Bash tool, but params carry a `description`, not a `command`.
        turn_records = [
            _make_turn([_make_command(tool_name="Bash", parameters={"description": "run the pytest suite"})])
        ]
        criterion = CommandExecutedCriterion(
            description="counts the bash record",
            tool_name="Bash",
            # Matches the shlex-stripped JSON (`{description: run ...}`) but NOT the
            # raw JSON (`{"description": "run ...}`); with the is_shell fix the raw
            # is the only haystack, so the command is not excluded.
            exclude_pattern=r"description:\s+run",
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_normalized_haystack_shares_the_raw_truncation_window(self):
        """Both haystacks describe the same <=2000-char window (no past-cap leak).

        Previously the raw haystack was truncated at 2000 chars but normalization
        ran over the FULL command, so quote-stripping could slide content from
        past the cap into the normalized haystack — a task relying on the
        2000-char bound changed verdict. Normalization now runs over the
        already-truncated window.
        """
        cmd = "bash -lc " + ("word " * 600) + "TARGET"  # TARGET sits well past 2000 chars
        haystacks = _match_haystacks(cmd, is_shell=True)
        assert all(len(h) <= _MAX_PATTERN_SEARCH_LEN for h in haystacks)
        assert not any("TARGET" in h for h in haystacks)

    def test_negative_assertion_not_dodged_by_quoting(self):
        """A quote-obfuscated retired call is still caught by a max_count=0 gate."""
        sandbox = MockSandbox()
        # `'uip' or users list` doesn't match `uip\s+or` on the raw text (a quote
        # sits right after uip), but the normalized form `uip or users list` does.
        obfuscated = "/bin/bash -lc \"'uip' or users list\""
        turn_records = [_make_turn([_make_command(tool_name="Bash", parameters={"command": obfuscated})])]
        criterion = CommandExecutedCriterion(
            description="must NOT use retired `uip or users list`",
            tool_name="Bash",
            command_pattern=r"uip\s+or\s+users\s+list",
            min_count=0,
            max_count=0,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 0.0


class TestShellToolHarnessAgnostic:
    """The `command_executed` criterion treats OpenHands `terminal` as a shell tool.

    A task author writes `tool_name: Bash` once; it must match an OpenHands
    `terminal` call the same way it matches a Claude/Codex `Bash` call. Non-shell
    filters (e.g. `Read`) keep exact-match semantics. Claude/Codex `Bash`
    behavior is byte-for-byte unchanged (covered here plus by `test_match_found`
    / `test_no_match`, which stand as the Bash regression).
    """

    def test_terminal_matches_bash_filter(self):
        """The headline fix: a `terminal` command satisfies a `tool_name: Bash` filter."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="terminal",
                        parameters={"command": "curl https://wttr.in/London"},
                        result_status="success",
                    ),
                ]
            )
        ]
        criterion = CommandExecutedCriterion(
            description="Agent used curl to fetch weather (via terminal)",
            tool_name="Bash",
            command_pattern=r"curl.*wttr\.in",
            min_count=1,
            require_success=True,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0
        assert result.error is None
        assert "1/1" in result.details

    def test_bash_filter_still_matches_bash(self):
        """Regression twin of `test_match_found`: Bash behavior is unchanged."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://wttr.in/London"},
                        result_status="success",
                    ),
                ]
            )
        ]
        criterion = CommandExecutedCriterion(
            description="Agent used curl to fetch weather",
            tool_name="Bash",
            command_pattern=r"curl.*wttr\.in",
            min_count=1,
            require_success=True,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0

    def test_non_shell_tool_name_stays_exact(self):
        """A non-shell `tool_name` filter (`Read`) keeps exact-match semantics.

        A shell command (`Bash`) must NOT satisfy a `Read` filter, and a `Read`
        command must satisfy it — proving exact-match is preserved and not
        accidentally broadened by the shell-tool loosening. (Also note: the
        symmetric case — a hypothetical `tool_name: terminal` filter matching a
        `Bash` command — is covered implicitly by `_tool_name_matches` in
        `test_unit_predicate`.)
        """
        sandbox = MockSandbox()

        bash_cmd = _make_turn([_make_command(tool_name="Bash", parameters={"command": "grep foo bar.txt"})])
        read_filter = CommandExecutedCriterion(
            description="Agent used Read tool",
            tool_name="Read",
            min_count=1,
        )
        assert SuccessChecker(sandbox).check(read_filter, turn_records=[bash_cmd]).score == 0.0

        read_cmd = _make_turn([_make_command(tool_name="Read", parameters={"file_path": "bar.txt"})])
        assert SuccessChecker(sandbox).check(read_filter, turn_records=[read_cmd]).score == 1.0

    def test_terminal_normalization_applies(self):
        """`_normalize_shell` quote-normalization runs for `terminal` commands.

        A `bash -lc "... 'single-quoted' ..."` wrapper form on a `terminal` call
        matches only against the logical (normalized) command — mirroring the
        `TestShellQuotingNormalization` wrapper shape but on `terminal` telemetry.
        """
        sandbox = MockSandbox()
        raw = (
            '/bin/bash -lc "uip is resources run list uipath-salesforce-slack '
            "'curated_channels?types=public_channel,private_channel' --output json\""
        )
        # Pattern allows optional backslash + optional DOUBLE quote, but no single
        # quote — only the normalized form (single quotes resolved) matches.
        yaml_pattern = r'uip\s+is\s+resources\s+run\s+list\s+\\?"?uipath-salesforce-slack\\?"?\s+\\?"?curated_channels'
        turn_records = [_make_turn([_make_command(tool_name="terminal", parameters={"command": raw})])]
        criterion = CommandExecutedCriterion(
            description="curated_channels list ran (via terminal)",
            tool_name="Bash",
            command_pattern=yaml_pattern,
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 1.0
        assert "1/1" in result.details

    def test_terminal_non_str_command_falls_back(self):
        """A `terminal` call without a str `command` is treated as non-shell.

        An `is_input`-only terminal action has no `command` string, so it must
        serialize its params to a JSON blob (no shell normalization) and get the
        `terminal(...)` JSON-form label — same guard the Bash path already uses.
        """
        sandbox = MockSandbox()
        turn_records = [_make_turn([_make_command(tool_name="terminal", parameters={"is_input": True})])]
        # A pattern that would only match a normalized shell form must NOT hit the
        # JSON blob (which is `{"is_input": true}`).
        criterion = CommandExecutedCriterion(
            description="no shell command present",
            tool_name="Bash",
            command_pattern=r"^curl",
            min_count=1,
        )
        result = SuccessChecker(sandbox).check(criterion, turn_records=turn_records)
        assert result.score == 0.0

        # With no pattern, the fallback command still matches the shell filter and
        # its label is the `terminal(...)` JSON form (not a raw-command label).
        label_criterion = CommandExecutedCriterion(
            description="any shell tool call",
            tool_name="Bash",
            min_count=1,
        )
        label_result = SuccessChecker(sandbox).check(label_criterion, turn_records=turn_records)
        assert label_result.score == 1.0
        assert "terminal(" in label_result.details

    def test_live_verdict_terminal_pass(self):
        """`live_verdict` and `_check_impl` agree on a `terminal`-only trajectory."""
        from coder_eval.criteria.command_executed import CommandExecutedChecker

        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="terminal",
                        parameters={"command": "curl https://wttr.in/London"},
                        result_status="success",
                    ),
                ]
            )
        ]
        criterion = CommandExecutedCriterion(
            description="curl via terminal (live)",
            tool_name="Bash",
            command_pattern=r"curl.*wttr\.in",
            min_count=1,
            max_count=None,
        )
        assert CommandExecutedChecker().live_verdict(criterion, turn_records) == "pass"

    def test_unit_predicate(self):
        """The shell-tool predicates classify tools correctly."""
        from coder_eval.criteria.command_executed import _is_shell_tool, _tool_name_matches

        assert _is_shell_tool("Bash")
        assert _is_shell_tool("terminal")
        assert not _is_shell_tool("Read")

        # Shell tools interchangeable (symmetric); non-shell filter is exact.
        assert _tool_name_matches("Bash", "terminal")
        assert _tool_name_matches("terminal", "Bash")
        assert _tool_name_matches("Bash", "Bash")
        assert not _tool_name_matches("Read", "Bash")
        assert _tool_name_matches("Read", "Read")
