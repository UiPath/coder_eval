"""Tests for ``match_on: simple_commands`` on CommandExecutedCriterion and its shell splitter."""

import gc
import time
from datetime import datetime

import pytest

from coder_eval.criteria import command_executed
from coder_eval.criteria.command_executed import CommandExecutedChecker
from coder_eval.models import CommandExecutedCriterion
from coder_eval.models.results import TurnRecord
from coder_eval.models.telemetry import CommandTelemetry
from coder_eval.shell_commands import SUBST, ShellSplitError, simple_commands


def _texts(text: str) -> set[str]:
    return {" ".join(argv) for argv in simple_commands(text)}


def _turn(*commands: str | list[str], tool_name: str = "Bash") -> list[TurnRecord]:
    return [
        TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="ok",
            commands=[
                CommandTelemetry(
                    tool_name=tool_name,
                    tool_id=f"tool-{i}",
                    timestamp=datetime.now(),
                    parameters={"command": c},
                    result_status="success",
                )
                for i, c in enumerate(commands)
            ],
        )
    ]


def _check(turns: list[TurnRecord], **fields):
    criterion = CommandExecutedCriterion(description="t", match_on="simple_commands", **fields)
    return CommandExecutedChecker().check(criterion, None, turn_records=turns)


VOICE = r"^(\S*/)?uip (maestro )?flow debug\b"


class TestHeredocs:
    def test_quoted_delimiter_drops_the_whole_body(self):
        assert _texts("cat > a.md <<'EOF'\nuip flow debug $(uip x)\nEOF\nuip flow pack") == {"cat", "uip flow pack"}

    def test_double_quoted_and_escaped_delimiters_are_quoted(self):
        assert _texts('cat <<"EOF"\nuip a\nEOF') == {"cat"}
        assert _texts("cat <<\\EOF\nuip a\nEOF") == {"cat"}

    def test_unquoted_body_keeps_only_substitutions(self):
        text = "cat > a.md <<EOF\nrun uip flow debug\nid: $(uip flow validate x) `uip y`\nEOF"
        assert _texts(text) == {"cat", "uip flow validate x", "uip y"}

    def test_dash_heredoc_strips_tabs_before_the_delimiter(self):
        assert _texts("cat <<-EOF\n\tuip a\n\tEOF\nuip b") == {"cat", "uip b"}

    def test_heredoc_fed_to_a_shell_is_a_script(self):
        assert _texts("bash -s <<'EOF'\nuip flow debug y\nEOF") == {"bash -s", "uip flow debug y"}
        assert _texts("sh <<EOF\nuip a && uip b\nEOF") == {"sh", "uip a", "uip b"}

    def test_heredoc_fed_to_a_shell_behind_a_prefix_is_a_script(self):
        assert _texts("X=1 bash <<'EOF'\nuip flow debug\nEOF") == {"X=1 bash", "uip flow debug"}
        assert _texts("env -u Y sh -s <<EOF\nuip a\nEOF") == {"env -u Y sh -s", "uip a"}

    def test_heredoc_fed_to_a_dash_c_shell_is_stdin(self):
        assert _texts("bash -c 'uip x' <<'EOF'\nuip a\nEOF") == {"uip x"}
        assert _texts("X=1 bash -c 'uip x' <<'EOF'\nuip a\nEOF") == {f"X=1 bash -c {SUBST}", "uip x"}

    def test_stdin_piped_into_a_shell_is_not_parsed(self):
        # Known limitation, documented in the guide: a piped script's commands do not count.
        assert _texts("cat <<'EOF' | bash\nuip a\nEOF") == {"cat", "bash"}
        assert _texts("echo 'uip a' | sh") == {"echo uip a", "sh"}

    def test_a_literal_heredoc_mark_is_a_word(self):
        assert _texts("grep -rn __HEREDOC_0__ src/") == {"grep -rn __HEREDOC_0__ src/"}
        assert _texts("cat <<EOF\nhi\nEOF\necho __HEREDOC_0__") == {"cat", "echo __HEREDOC_0__"}

    def test_heredoc_fed_to_python_is_dropped(self):
        assert _texts("python3 - <<'PY'\nimport os; os.system('uip flow debug')\nPY") == {"python3 -"}

    def test_two_heredocs_on_one_line(self):
        assert _texts("cat <<A <<'B'\nuip a\nA\nuip b\nB\nuip c") == {"cat", "uip c"}

    def test_here_string_is_a_redirection(self):
        assert _texts('cat <<<"$x" | uip w') == {"cat", "uip w"}


class TestSubstitutions:
    def test_nested_substitutions_are_commands(self):
        assert _texts("x=$(uip a $(uip b)) && echo `uip c`") == {
            f"x={SUBST}",
            f"echo {SUBST}",
            f"uip a {SUBST}",
            "uip b",
            "uip c",
        }

    def test_process_substitution(self):
        assert _texts("diff <(uip a) <(uip b) >/dev/null") == {f"diff {SUBST} {SUBST}", "uip a", "uip b"}

    def test_substitution_inside_double_quotes(self):
        assert _texts('echo "id=$(uip a)"') == {f"echo id={SUBST}", "uip a"}

    def test_arithmetic_is_not_a_command(self):
        assert _texts("echo $((1 + 2))") == {"echo $((1 + 2))"}

    def test_single_quotes_do_not_expand(self):
        assert _texts("echo '$(uip a)'") == {"echo $(uip a)"}

    def test_shell_dash_c_string_is_parsed(self):
        assert _texts('bash -lc "cd /x && uip flow debug \\"a b\\""') == {"cd /x", "uip flow debug a b"}
        assert _texts("sh -c 'uip a; uip b'") == {"uip a", "uip b"}

    def test_argv_joined_dash_c_form(self):
        assert _texts("bash -lc uip flow debug x") == {"uip flow debug x"}

    def test_eval_is_parsed(self):
        assert _texts("eval 'uip a; uip b'") == {"uip a", "uip b"}


class TestWrappers:
    def test_wrappers_with_flags_are_stripped(self):
        text = "timeout -s KILL 60 sudo -u me nohup nice -n 5 stdbuf -oL uip flow debug a"
        assert _texts(text) == {"uip flow debug a"}

    def test_exec_command_time_builtin(self):
        assert _texts("exec uip a; command uip b; time -p uip c; builtin echo d") == {
            "uip a",
            "uip b",
            "uip c",
            "echo d",
        }

    def test_xargs_with_flags(self):
        assert _texts("ls | xargs -I{} -n1 -P 4 uip y {}") == {"ls", "uip y {}"}

    def test_find_exec_is_its_own_command(self):
        assert _texts(r"find . -name x -exec timeout 5 uip flow debug {} \; -print") == {
            "find . -name x -print",
            "uip flow debug {}",
        }
        assert _texts("find . -exec rm {} + ; find . -execdir uip a ';'") == {"find .", "rm {}", "uip a"}

    def test_find_exec_shell_script_is_parsed(self):
        assert _texts("find . -name '*.flow' -exec sh -c 'uip flow validate \"$1\"' _ {} \\;") == {
            "find . -name *.flow",
            "uip flow validate $1",
        }

    def test_assignment_before_a_shell_keeps_the_assignment_and_parses_the_script(self):
        assert _texts("X=1 env -u Y bash -lc 'uip a && uip b'") == {f"X=1 env -u Y bash -lc {SUBST}", "uip a", "uip b"}

    def test_assignment_before_find_exec(self):
        assert _texts("X=1 find . -exec uip a {} +") == {"X=1 find .", "uip a {}"}

    def test_assignments_and_env_stay_visible(self):
        assert _texts("env -u UIPATH_STUDIO_PID uip rpa build; X=1 Y=2 uip x") == {
            "env -u UIPATH_STUDIO_PID uip rpa build",
            "X=1 Y=2 uip x",
        }

    def test_stripping_stops_at_an_assignment(self):
        assert _texts("timeout 5 X=1 uip a") == {"X=1 uip a"}


class TestSplitting:
    def test_operators_split(self):
        assert _texts("uip a; uip b && uip c || uip d | uip e & uip f\nuip g |& uip h") == {
            f"uip {c}" for c in "abcdefgh"
        }

    def test_subshell_and_group(self):
        assert _texts("(cd x && uip a) ; { uip b; }") == {"cd x", "uip a", "uip b"}

    def test_keywords_are_not_commands(self):
        assert _texts("if [ -f x ]; then uip z; elif true; then :; else uip y; fi") == {
            "[ -f x ]",
            "uip z",
            "true",
            ":",
            "uip y",
        }
        assert _texts("while ! uip ping; do sleep 1; done") == {"uip ping", "sleep 1"}

    def test_loop_header_is_not_a_command_and_variables_are_not_expanded(self):
        assert _texts("for t in 'flow debug' b; do uip $t; done") == {"uip $t"}

    def test_redirections_are_dropped(self):
        assert _texts("uip a > out.txt 2>&1; uip b 2>/dev/null >>log &> all") == {"uip a", "uip b"}

    def test_backslash_continuation(self):
        assert _texts("uip flow \\\n  debug x") == {"uip flow debug x"}


class TestQuotingAndComments:
    def test_quotes_are_resolved(self):
        assert _texts("uip 'flow' \"debug\" a\\ b") == {"uip flow debug a b"}

    def test_quoted_operators_are_words(self):
        assert _texts("echo ';' 'a|b' \"x && y\" a\\;b; uip z") == {"echo ; a|b x && y a;b", "uip z"}

    def test_quoted_redirection_characters_are_words(self):
        assert _texts("grep '>' file.txt; uip a \\< b \"<x>\"") == {"grep > file.txt", "uip a < b <x>"}

    def test_echo_string_is_an_argument(self):
        assert _texts('echo "uip flow debug x"') == {"echo uip flow debug x"}

    def test_comments_are_dropped(self):
        assert _texts("uip a # then uip flow debug\n# uip b\nuip c") == {"uip a", "uip c"}

    def test_hash_inside_a_word_is_not_a_comment(self):
        assert _texts("echo a#b $#") == {"echo a#b $#"}

    def test_the_splitters_own_marks_are_unparseable(self):
        for ch in ("\ue000", "\ue007", "\ue0ff"):
            with pytest.raises(ShellSplitError):
                simple_commands(f"echo a{ch}b")

    def test_other_private_use_characters_are_words(self):
        # Codex citation marks, seen in stored nightly commands.
        assert _texts("echo 'x \ue200cite\ue202turn1view0\ue201' | uip a") == {
            "echo x \ue200cite\ue202turn1view0\ue201",
            "uip a",
        }

    def test_unterminated_quote_is_unparseable(self):
        with pytest.raises(ShellSplitError):
            simple_commands("echo 'oops")
        with pytest.raises(ShellSplitError):
            simple_commands("echo $(uip a")


class TestLinearTime:
    @pytest.mark.parametrize(
        "unit",
        [
            "uip flow debug a && echo 'x;y' | tee log\n",
            "cat <<EOF\n" + "line $(uip a) text\n" * 20 + "EOF\n",
            'x=$(echo "$(uip a `uip b`)")\n',
            "# " + "c" * 1990 + "\n",
            # Pumped wrapper and keyword runs: stripping them must not re-slice per word.
            "sudo -a ",
            "! ",
            "nohup ",
            "timeout -s KILL 5 ",
        ],
    )
    def test_time_grows_linearly(self, unit):
        def cost(chars: int) -> float:
            text = unit * (chars // len(unit) + 1)
            best = float("inf")
            gc.disable()  # a collection pause is not the splitter's cost
            try:
                for _ in range(5):
                    start = time.process_time()
                    simple_commands(text)
                    best = min(best, time.process_time() - start)
            finally:
                gc.enable()
            return best

        small, large = cost(20_000), cost(80_000)
        assert large < 8 * small + 0.01  # x4 input: linear, with slack for noise


class TestChecker:
    def test_heredoc_mention_does_not_trip_a_guard(self):
        turns = _turn("cat > notes.md <<'EOF'\nNext: uip flow debug\nEOF", "echo 'uip flow debug' >> x")
        assert _check(turns, command_pattern=VOICE, min_count=0, max_count=0).score == 1.0

    def test_real_call_trips_the_guard(self):
        turns = _turn("cd proj && timeout 60 uip maestro flow debug f.flow --output json")
        result = _check(turns, command_pattern=VOICE, min_count=0, max_count=0)
        assert result.score == 0.0
        assert "uip maestro flow debug f.flow --output json" in result.details

    def test_default_match_on_keeps_raw_matching(self):
        turns = _turn("cat > notes.md <<'EOF'\nuip flow debug\nEOF")
        criterion = CommandExecutedCriterion(
            description="t", command_pattern="uip flow debug", min_count=0, max_count=0
        )
        assert criterion.match_on == "command_text"
        assert CommandExecutedChecker().check(criterion, None, turn_records=turns).score == 0.0

    def test_counts_per_simple_command(self):
        turns = _turn("uip solution pack a && uip solution pack b", "echo 'uip solution pack'")
        result = _check(turns, command_pattern=r"^uip solution pack\b", min_count=2)
        assert result.score == 1.0
        assert "Matched 2/2" in result.details
        assert _check(turns, command_pattern=r"^uip solution pack\b", max_count=1).score == 0.0

    def test_exclude_applies_per_simple_command(self):
        turns = _turn("uip a --dry-run; uip a")
        result = _check(turns, command_pattern="^uip a", exclude_pattern="--dry-run", min_count=1, max_count=1)
        assert result.score == 1.0

    def test_env_assignment_visible_to_the_pattern(self):
        turns = _turn("env -u UIPATH_STUDIO_PID uip rpa build .")
        assert _check(turns, command_pattern=r"^env -u UIPATH_STUDIO_PID uip rpa build").score == 1.0

    def test_unparseable_falls_back_to_raw_and_says_so(self):
        turns = _turn("echo 'oops; uip flow debug x")
        result = _check(turns, command_pattern="uip flow debug", min_count=1)
        assert result.score == 1.0
        assert "1 call(s) matched on raw text (1 not splittable into simple commands)" in result.details

    def test_literal_heredoc_mark_does_not_break_a_guard(self):
        turns = _turn("grep -rn __HEREDOC_0__ src/")
        assert _check(turns, command_pattern=VOICE, min_count=0, max_count=0).score == 1.0

    def test_splitter_exception_falls_back_to_raw_and_says_so(self, monkeypatch):
        def broken(_text: str) -> list[list[str]]:
            raise IndexError("list index out of range")

        monkeypatch.setattr(command_executed, "simple_commands", broken)
        command_executed._simple_command_texts.cache_clear()
        try:
            turns = _turn("echo 'uip flow debug' # splitter-exception probe")
            result = _check(turns, command_pattern="uip flow debug", min_count=0, max_count=0)
        finally:
            command_executed._simple_command_texts.cache_clear()
        assert result.error is None
        assert result.score == 0.0  # raw text matched, as with command_text
        assert "1 call(s) matched on raw text (1 splitter error IndexError)" in result.details

    @pytest.mark.parametrize("shell", ["bash", "yash", "fish", "/usr/bin/zsh"])
    def test_dash_c_unwraps_in_both_modes(self, shell):
        turns = _turn(f"{shell} -c 'uip flow debug x'")
        assert _check(turns, command_pattern=VOICE, min_count=1).score == 1.0
        criterion = CommandExecutedCriterion(description="t", command_pattern=VOICE, min_count=1)
        assert CommandExecutedChecker().check(criterion, None, turn_records=turns).score == 1.0

    @pytest.mark.parametrize(
        "command",
        [
            "\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command 'echo uip flow debug'",
            "pwsh -Command \"Write-Output 'x'; uip flow debug\"",
            "cd x; powershell.exe -c 'uip flow debug'",
            "$env:UIPATH_STUDIO_PID = $null; uip flow debug x",
            "Get-Content a.txt; uip flow debug x",
        ],
    )
    def test_powershell_falls_back_to_raw(self, command):
        result = _check(_turn(command), command_pattern="uip flow debug", min_count=1)
        assert result.score == 1.0
        assert "not POSIX shell" in result.details

    def test_powershell_tool_falls_back_to_raw(self):
        turns = _turn("Write-Host 'a'; uip flow debug", tool_name="ExecutePowershellCommand")
        result = _check(turns, command_pattern="uip flow debug", min_count=1)
        assert "not POSIX shell" in result.details

    def test_argv_list_is_split(self):
        turns = _turn(["/bin/zsh", "-lc", "cat <<'EOF'\nuip flow debug\nEOF\nuip flow pack"])
        assert _check(turns, command_pattern=VOICE, min_count=0, max_count=0).score == 1.0
        assert _check(turns, command_pattern=r"^uip flow pack$").score == 1.0

    def test_non_shell_tool_json_is_matched_as_before(self):
        turns = [
            TurnRecord(
                iteration=1,
                user_input="test",
                agent_output="ok",
                commands=[
                    CommandTelemetry(
                        tool_name="Write",
                        tool_id="w",
                        timestamp=datetime.now(),
                        parameters={"file_path": "a.sh", "content": "uip flow debug"},
                        result_status="success",
                    )
                ],
            )
        ]
        assert _check(turns, tool_name="Write", command_pattern="uip flow debug").score == 1.0

    def test_live_verdict_counts_like_the_score(self):
        turns = _turn("uip a; uip a", "cat <<'EOF'\nuip a\nEOF")
        criterion = CommandExecutedCriterion(
            description="t", match_on="simple_commands", command_pattern="^uip a$", max_count=1
        )
        assert CommandExecutedChecker().live_verdict(criterion, turns) == "fail"
        guard = CommandExecutedCriterion(
            description="t", match_on="simple_commands", command_pattern="^uip b", min_count=0, max_count=0
        )
        assert CommandExecutedChecker().live_verdict(guard, turns) == "undecided"

    def test_legacy_command_not_executed_keeps_match_on(self):
        from coder_eval.models import SandboxConfig, TaskDefinition

        td = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            agent=None,
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[
                {
                    "type": "command_not_executed",
                    "description": "no debug",
                    "command_pattern": VOICE,
                    "match_on": "simple_commands",
                }
            ],
        )
        criterion = td.success_criteria[0]
        assert isinstance(criterion, CommandExecutedCriterion)
        assert (criterion.match_on, criterion.max_count) == ("simple_commands", 0)
